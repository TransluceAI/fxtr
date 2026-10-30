"""The per-generation ops engine: record-checked effectful grid operations.

One :class:`OpsEngine` serves one *generation* of one task cell (see
``notes/execution_semantics.md``). It is the substance behind the run
capability: every effectful op from the wire (`runner_api.py`) lands here,
and the engine performs, in order —

1. **Spec resolution**: the incoming ``ViewSel`` is rebuilt into a true
   spec (`spec_registry.py`) and resolved to an absolute storage location.
   Spec ``Param``\\ s resolve against :attr:`OpsEngine.holder_coords` — the
   executing cell's flattened coordinates, the engine's *only*
   cell-dependent input, consulted at op time and never at
   canonicalization time (P2 preserved).
2. **The universal record-check**: hits serve the recorded outcome without
   touching live state; misses go live and record. The in-memory record is
   loaded from the state db at construction, so *resume is just
   constructing the engine again*. Hit-mismatch rules: a write of a
   different value or an open with a different config against your own
   record is ``NondeterminismError``.
3. **Integrity checks**: read-xor-write per grid per generation
   (``ReadWriteExclusionError``); grant-mode enforcement on writes; opens
   are create-or-attach on canonical config (``GridConfigMismatchError``
   on live mismatch); handle grids are attach-only; checkpoint grids only
   open within the generation's own internals.
4. **Recording**: subject-version interning + observation edges + grid
   assertion claims (would-create, on write-authority opens) through the
   :class:`~fxtr_experiment.state_store.
   WorkflowSession` (whose combined helpers keep the liveness invariant).
   The record doubles as the generation's *claims*: ``wrote_cell`` edges
   claim written values, assertion rows claim asserted grids
   (``notes/grounding.md``), and both expire with the generation.

Around all of that sits the **request-obligation contract**
(:func:`_effectful_op`, implementing *request = observation* and the
raise-time half of the poisoning contract): every public op registers an
obligation synchronously on entry (the commitment point — task-side
cancellation cannot retract it), persists any workflow error it raises as
a failure record (`storage.py`; mirrored on
:attr:`OpsEngine.run_failures`), records a fault-class
``RequestAbortedError`` failure if it is cancelled before resolution
(never blocking on in-flight work), and discharges the obligation either
way. The executor's run-completion gate is
:meth:`OpsEngine.wait_obligations_drained` + an empty ``run_failures``.

Every record-miss task-output read is served through the **demand hook**
(required at construction): in production that is the executor's
``demand``, which returns validated, currency-checked results; test
harnesses wire simpler hooks (the ops-engine tests use a status-only
serve, accepting its weaker semantics). The engine itself never checks
currency — it serves and records whatever the hook vouches for.

**Steps** run through :meth:`OpsEngine.run_step`: checkpoint entries are
the record's step half — same discipline as observations (subject-keyed
hit/miss, hit-mismatch = nondeterminism, frontier guard, `visited`
membership, cascade turnover), different payload and consumers (the
recorded invocation is the step's service type id + args fingerprint;
entries never participate in currency). A miss runs the step under the
injected :class:`StepHost`'s authority, with a :class:`StepCapabilityImpl`
mounted in an isolated per-step scope (``connect`` authority = this run's
handle-grant reads; failures poison the invocation at raise time).

The engine's method signatures match the :class:`~fxtr_experiment.
view_runtime.ViewBackend` seam, so runtime view objects can run directly
over it (which is how the tests drive it).
"""

from __future__ import annotations

import dataclasses
import functools
import uuid
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Mapping, Protocol, TypeVar

if TYPE_CHECKING:
    from fxtr2.services.service_cap_common import FxtrServiceData

import anyio
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import Entity, EntityRef, FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr2.data.system_entities import define_type

from fxtr_experiment_task._ns import WORKFLOWS3_NS

from fxtr_experiment.run_log import RunLogSink
from fxtr_experiment.watch_api import LogEntryKind, StepRef
from fxtr_experiment.encoding import (
    GridConfigEntity,
    StepPayloadEntity,
    encode_coords,
    encode_key_path,
    frozen_cbor_to_service_data,
    service_data_to_frozen_cbor,
    store_config,
    store_step_payload,
)
from fxtr_experiment_task.errors import (
    CellFaultError,
    EncodingError,
    CheckpointCorruptionError,
    ConnectAuthorityError,
    DuplicateStepError,
    GatherFailedError,
    GridConfigMismatchError,
    InternalRunnerError,
    NondeterminismError,
    ReadWriteExclusionError,
    RequestAbortedError,
    StepInvocationFailedError,
    StepRaisedError,
    ViewAxisError,
    WorkflowError,
    WriteConflictError,
)
from fxtr_experiment_task.model import (
    AxisKind,
    AxisSpec,
    CheckpointGridConfig,
    CoordValue,
    DataFrameGridConfig,
    DataGridConfig,
    FrameSchema,
    GridConfig,
    HandleGridConfig,
    LiteralCoord,
    OutputSpec,
    Param,
    SliceMode,
    SubtreeGridConfig,
    TaskGridConfig,
    rule_reference_paths,
)
from fxtr_experiment_task.runner_api import (
    StepCapabilityProto,
    ViewGrant,
    ViewSel,
    ViewToken,
    WiredArg,
    WiredCoord,
    WorkflowStepFnProto,
)
from fxtr_experiment.spec_registry import SpecRegistry
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import (
    FAILURE_ERROR,
    FAILURE_FAULT,
    OBS_KIND_CELL_VALUE,
    OBS_KIND_GRID_CONFIG,
    OBS_KIND_WROTE_CELL,
    REQ_KIND_DATA,
    REQ_KIND_TASK_OUTPUT,
)
from fxtr_experiment_task.view_spec import (
    CoordViewSpec,
    GridKind,
    StoredViewSpec,
    SubtreeViewSpec,
    TaskViewSpec,
    open_child,
)

Subject = tuple[str, str, str]
"""In-memory record key: (grid key-path text, coords text, output name) —
'' sentinels exactly as in the ``subject_versions`` table."""

DemandHook = Callable[[str, str], Awaitable[Mapping[str, FxtrID]]]
"""Executor callback: produce a demanded task cell's validated results.

Called with the target's (key-path text, flattened-coords text) when a
record-miss read addresses a task output — executing/resuming the target if
needed. Returns the cell's per-output results as a **value-pinned fact**: a
(generation, results) pair the executor's currency gate checked
complete-and-fresh at a single snapshot. The engine serves and records the
demanded output from exactly this map, never re-reading live state — check
and read are one operation, so nothing can turn over between them. On
failure the hook raises the appropriate workflow error itself (which then
poisons the demanding generation per class)."""


ReadGateHook = Callable[[str, str], Awaitable[FxtrID | None]]
"""Executor callback gating record-miss data/handle-cell reads.

Called with the subject's (key-path text, flattened-coords text); returns
the cell's value id from the same snapshot the gate judged (serve-or-fail
— `WorkflowSession.gated_read_cell`), ``None`` for an empty cell (the
engine raises its ordinary missing-cell fault), or raises the gate's
fault (``UngroundedSubjectError``, which then poisons the reading
generation as any not-ready dependency). ``None`` hook = ungated reads
(bare-engine test harnesses; the executor always wires one, carrying its
policy)."""


class StepConnector(Protocol):
    """The slice of the binding registry a step host carries: spawn a live
    service for a bound handle key (`bindings.BindingHost`
    satisfies this structurally — typed as a local protocol to avoid an
    import cycle)."""

    async def spawn(self, handle: FxtrID, *, into_scope: FxtrScope) -> ServiceRef[UnknownService]:
        """See `bindings.BindingHost.spawn`."""
        ...


@dataclasses.dataclass(frozen=True)
class StepHost:
    """The authority a run_step miss path needs — and nothing else.

    Injected at engine construction (the host-style decision from the
    chunk 7 design round): the engine owns the whole miss path — per-step
    scope, capability, invocation, persist-inside-scope, record — and the
    host supplies what the engine deliberately doesn't have.

    Attributes:
        scope: The scope per-step scopes are isolated children of (the
            executor passes the run scope) — it must reach the binding
            registry's scope so spawned/connected refs resolve.
        bindings: The runner's handle → connector registry (steps'
            ``connect`` resolves through it).
    """

    scope: FxtrScope
    bindings: StepConnector


def new_generation_id() -> str:
    """Mint a public generation id (an opaque uuid string)."""
    return str(uuid.uuid4())


def _gather_failed(failures: "list[tuple[Subject, BaseException]]") -> GatherFailedError:
    """Synthesize a gather's one escaping signal from its per-cell causes.

    Every entry is a cell whose failure was already handled per-request
    (recorded runner-side; nothing on the user surface) — the signal
    lists them all, sorted by subject for a deterministic message
    (resolution is concurrent; arrival order means nothing).
    """
    lines = "; ".join(
        f"{g}{c}" + (f" output {o!r}" if o else "") + f": {type(e).__name__}: {e}"
        for (g, c, o), e in sorted(failures, key=lambda f: f[0])
    )
    return GatherFailedError(f"gather failed on {len(failures)} cell(s): {lines}")


def resolve_templates(
    path: tuple, target_axes: tuple, holder_coords: tuple[CoordValue, ...]
) -> tuple[str, str]:
    """Resolve fully-bound templates to one absolute storage location.

    Shared by the engine (sliced specs, via :meth:`OpsEngine._resolve_cell`)
    and the executor (stored rule slices — same template shape, same
    holder frame). ``Param(i)`` resolves against ``holder_coords``.

    Args:
        path: Path-step templates (each with ``.key`` and ``.cell``).
        target_axes: The target's own-axis templates.
        holder_coords: The executing cell's flattened coordinates.

    Returns:
        ``(key_path_text, flattened_coords_text)`` in canonical form.

    Raises:
        ViewAxisError: A kept axis remains (the selection is not
            single-cell), or a Param position exceeds the holder's
            flattened arity (a wiring bug upstream).
    """
    keys = tuple(step.key for step in path)
    values: list[CoordValue] = []
    templates = [t for step in path for t in step.cell] + list(target_axes)
    for t in templates:
        if isinstance(t, LiteralCoord):
            values.append(t.value)
        elif isinstance(t, Param):
            if not 0 <= t.position < len(holder_coords):
                raise ViewAxisError(
                    f"template binds holder axis {t.position}; this cell has "
                    f"{len(holder_coords)} flattened coordinates"
                )
            values.append(holder_coords[t.position])
        else:
            raise ViewAxisError("selection leaves axes open; a single cell is required")
    return encode_key_path(keys), encode_coords(values)


def _invocation_failed(
    grid_path: str, coords: str, cause: WorkflowError
) -> StepInvocationFailedError:
    """Build the (never-recorded) invocation-failure signal, naming the
    already-recorded cause for the task-side transit form."""
    return StepInvocationFailedError(
        f"step invocation at {grid_path}{coords} was poisoned by"
        f" {type(cause).__name__}: {cause}"
    )


_OpFn = TypeVar("_OpFn", bound=Callable)


def _effectful_op(method: _OpFn) -> _OpFn:
    """Wrap one public engine op with the request-obligation contract.

    Implements *request = observation* (``notes/execution_semantics.md``)
    for this op:

    - The **commitment point** is the first statement, synchronous by
      construction: anyio cancellation can only land at an ``await``, so
      once the wrapper is entered the obligation is registered and
      task-side cancellation cannot retract it (this matters in-process,
      where dispatch runs in the caller's task and cancellation *does*
      propagate — RPC drops it anyway).
    - A :class:`WorkflowError` raised by the body is **persisted at raise
      time** (the poisoning contract: the failure stands even if task code
      catches the exception or ignores the result) and mirrored on
      :attr:`OpsEngine.run_failures`, then re-raised. So is any
      **unexpected internal exception** (error-class, real class name in
      the row): poisoning covers op failures of any type, else catching
      the wrapped form could complete cleanly around a half-done op.
    - A committed op **cancelled before resolution** — the task dropped
      its request, or the whole run is being cancelled externally — is
      recorded as a fault-class :class:`RequestAbortedError` failure and
      the cancellation re-raised immediately. The op does *not* run to
      completion (no shield around the body: cancelling a runner must
      never block on in-flight work, e.g. a demand of a long upstream
      task); the run can then only end ``interrupted``, and a later run
      re-attempts the op at the frontier. The one narrow shield is inside
      :meth:`OpsEngine._record_run_failure`, so the failure row itself
      survives a pending cancellation.

    Resolution — value, failure, or abort — discharges the obligation; the
    run-completion gate awaits :meth:`OpsEngine.wait_obligations_drained`.
    """

    @functools.wraps(method)
    async def wrapper(self: "OpsEngine", *args: object, **kwargs: object) -> object:
        self._register_obligation()  # sync: the commitment point
        try:
            return await method(self, *args, **kwargs)
        except WorkflowError as exc:
            await self._record_run_failure(exc)
            raise
        except anyio.get_cancelled_exc_class():
            await self._record_run_failure(
                RequestAbortedError(f"request {method.__name__} cancelled before resolution")
            )
            raise
        except Exception as exc:
            # An unexpected internal error (an engine/storage bug, not the
            # workflow taxonomy). Poisoning covers op failures of ANY type:
            # without this arm, task code catching the wrapped exception
            # could complete cleanly around an op that failed mid-effect.
            # Error-class (a defect someone must look at, not a not-ready
            # dependency); the real class name/message land in the row.
            await self._record_run_failure(exc)
            raise
        finally:
            self._discharge_obligation()

    return wrapper  # type: ignore[return-value]


class OpsEngine:
    """Record-checked ops for one generation of one task cell.

    Construct via :meth:`load`. Not thread-safe (one generation runs its
    outer level in one anyio task tree; the state store serializes db
    access underneath).

    Attributes:
        visited: The subjects touched by *this run*, for the safety pass
            (compare against the full record; see execution semantics).

    Private state (documented because the invariants matter):

    - ``_record``: the generation's op record, subject → (kind,
      fingerprint). Superset of ``visited`` on a resumed run; every miss
      appends here *after* its durable row commits, so the in-memory record
      never claims more than the db.
    - ``_grid_side``: read-xor-write bookkeeping, key-path → "read"/"write".
      Derived from the record at load; grid_config observations are
      neutral (opens are neither).
    - ``_holder_coords``: the executing cell's flattened coordinates — the
      engine's only cell-dependent input, used solely by
      :meth:`_resolve_cell`.
    """

    def __init__(
        self,
        session: WorkflowSession,
        registry: SpecRegistry,
        *,
        exec_id: int,
        generation: str,
        holder_coords: tuple[CoordValue, ...],
        demand_hook: DemandHook,
        read_gate: ReadGateHook | None = None,
        frozen: bool = False,
        step_host: StepHost | None = None,
        run_log: "RunLogSink | None" = None,
    ) -> None:
        """Bare constructor — prefer :meth:`load`, which also rebuilds the
        durable record and the read/write side bookkeeping."""
        self._session = session
        self._registry = registry
        self._exec_id = exec_id
        self._generation = generation
        self._holder_coords = holder_coords
        self._demand_hook = demand_hook
        self._read_gate = read_gate
        self._frozen = frozen
        self._step_host = step_host
        self._run_log = run_log
        self._checkpoints: dict[Subject, tuple[FxtrID, FxtrID, FxtrID]] = {}
        self._steps_called: set[Subject] = set()  # one run_step per cell per run
        self._handle_keys_read: set[FxtrID] = set()  # connect authority (this run)
        self._record: dict[Subject, tuple[int, FxtrID]] = {}
        self._grid_side: dict[str, str] = {}  # key-path -> "read" | "write"
        self.visited: set[Subject] = set()
        self._obligations = 0  # committed-but-unresolved ops (this run)
        self._drained = anyio.Event()
        self._drained.set()  # vacuously drained until the first op commits
        self.run_failures: list[tuple[int, str, str]] = []
        """Failures recorded by this engine instance (= this run), as
        ``(FAILURE_* kind, error class name, message)`` — the in-memory
        mirror of the rows persisted at raise time. The executor derives
        the run's terminal status from this set (empty + clean return =
        complete; any FAILURE_ERROR → errored; only FAILURE_FAULT →
        interrupted)."""
        self.assertions: set[tuple[str, FxtrID]] = set()
        """Would-create claims made by this run's opens, as ``(grid
        key-path, config id)`` — the in-memory mirror of the generation's
        ``grid_assertions`` rows (this run's contribution only; the
        durable set is monotone across runs). A frozen engine *only*
        collects here (never inserts), and the safety pass compares this
        against the stored set."""

    # -- request obligations (request = observation) ------------------------------

    @property
    def obligations_outstanding(self) -> int:
        """Committed-but-unresolved ops right now (0 = drained)."""
        return self._obligations

    def _register_obligation(self) -> None:
        """Commit one op (sync — see :func:`_effectful_op`)."""
        if self._obligations == 0:
            self._drained = anyio.Event()
        self._obligations += 1

    def _discharge_obligation(self) -> None:
        """Resolve one committed op (value or failure)."""
        self._obligations -= 1
        if self._obligations == 0:
            self._drained.set()

    async def wait_obligations_drained(self) -> None:
        """Block until every committed op has resolved.

        The run-completion gate: a run is not over — however its task fn
        returned — until this returns *and* :attr:`run_failures` has been
        consulted (request = observation; a pending request that resolves
        to a fault poisons the run exactly as if it had been awaited).
        """
        while self._obligations:
            await self._drained.wait()

    async def _record_run_failure(self, exc: Exception) -> None:
        """Persist one failure at raise time (+ the in-memory mirror).

        Classification is from the typed exception, here on the raising
        side — ``CellFaultError`` and subclasses are faults; every other
        exception (workflow errors *and* unexpected internal errors) is
        error-class (see ``errors.py`` and :func:`_effectful_op`).
        Poisoning is registered exactly once, at the raise site that
        *originates* an error — ``StepInvocationFailedError`` and
        ``GatherFailedError`` are the types skipped here, by their
        contracts: both are signals constructed only for
        already-recorded causes. The db write runs under a
        *narrow* shield: this method is called with
        a cancellation possibly already pending (the abort path, or a
        workflow error raised while being cancelled), where an unshielded
        ``await`` would raise immediately and lose the row. The write is
        one bounded insert — nothing long-running hides in here.
        """
        if isinstance(exc, (StepInvocationFailedError, GatherFailedError)):
            return  # signals, not poisonings: their causes' rows already exist
        kind = FAILURE_FAULT if isinstance(exc, CellFaultError) else FAILURE_ERROR
        with anyio.CancelScope(shield=True):
            await self._session.record_failure(self._exec_id, kind, type(exc).__name__, str(exc))
        self.run_failures.append((kind, type(exc).__name__, str(exc)))

    @classmethod
    async def load(
        cls,
        session: WorkflowSession,
        registry: SpecRegistry,
        *,
        exec_id: int,
        generation: str,
        holder_coords: tuple[CoordValue, ...],
        demand_hook: DemandHook,
        read_gate: ReadGateHook | None = None,
        frozen: bool = False,
        step_host: StepHost | None = None,
        run_log: RunLogSink | None = None,
    ) -> "OpsEngine":
        """Construct the engine, loading the generation's durable record.

        Resume after a crash/eviction is exactly this: the record's entries
        become hits and the run replays to its frontier.

        Args:
            session: The open two-file durable session.
            registry: The run's token ↔ true-spec registry (tokens are
                per-run; a resumed run mints fresh grants).
            exec_id: The generation's private db key (FK target).
            generation: The generation's public uuid (record-load key).
            holder_coords: The executing cell's flattened coordinates.
            demand_hook: The demand hook every record-miss task-output
                read is served through (required; see :data:`DemandHook`).
            frozen: A frozen engine refuses the frontier: any record-miss
                effectful op raises ``NondeterminismError`` instead of
                going live. The safety pass's engine ("every effectful op
                is a hit" — execution semantics), making the pass pure
                compute by construction.
            step_host: The authority ``run_step`` misses need (see
                :class:`StepHost`). ``None`` is legitimate only for
                engines whose misses can't happen (frozen) or whose runs
                never step; a hostless miss is an
                :class:`~fxtr_experiment.errors.InternalRunnerError`.
            run_log: The run's log sink (`run_log.py`): step ``log()``
                entries and the engine's lifecycle notes land there.
                ``None`` discards — the safety pass's structural silence
                (and hostless test engines); the buffer is runtime-only
                display state, never system state.

        Returns:
            The engine, with the record and read/write sides rebuilt.
        """
        engine = cls(
            session,
            registry,
            exec_id=exec_id,
            generation=generation,
            holder_coords=holder_coords,
            demand_hook=demand_hook,
            read_gate=read_gate,
            frozen=frozen,
            step_host=step_host,
            run_log=run_log,
        )
        for grid_path, coords, step_type_id, args_id, value_id in await session.load_checkpoints(
            exec_id
        ):
            engine._checkpoints[(grid_path, coords, "")] = (step_type_id, args_id, value_id)
        for grid_path, coords, output, fingerprint, kind in await session.load_record(generation):
            engine._record[(grid_path, coords, output)] = (kind, fingerprint)
            if kind == OBS_KIND_WROTE_CELL:
                engine._grid_side[grid_path] = "write"
            elif kind == OBS_KIND_CELL_VALUE:
                engine._grid_side.setdefault(grid_path, "read")
        return engine

    # -- resolution -------------------------------------------------------------

    def _resolve_cell(self, spec: StoredViewSpec) -> tuple[str, str]:
        """Resolve a fully-bound spec via :func:`resolve_templates` against
        this cell's flattened coordinates — the one place per-cell data
        enters (op time, never canonicalization time). Coord specs never
        arrive here: every op kind-checks first (coord views have no
        storage target to resolve)."""
        return resolve_templates(spec.path, spec.target_axes, self._holder_coords)

    # -- record + integrity helpers ----------------------------------------------

    def _guard_frontier(self, subject: Subject) -> None:
        """Gate a record-miss op about to go live.

        Raises:
            NondeterminismError: This engine is frozen (the safety pass):
                reaching the frontier means the replay performed an op the
                record never saw.
        """
        if self._frozen:
            raise NondeterminismError(
                f"safety pass reached the frontier: unrecorded op on {subject}"
            )

    def _mark(self, grid_path: str, side: str) -> None:
        """Claim a grid for reading or writing (read-xor-write, op level).

        Args:
            grid_path: The grid's key-path text.
            side: ``"read"`` or ``"write"``.

        Raises:
            ReadWriteExclusionError: The generation already used the grid
                from the opposite side.
        """
        existing = self._grid_side.get(grid_path)
        if existing is not None and existing != side:
            raise ReadWriteExclusionError(
                f"generation already {existing}s grid {grid_path}; cannot {side} it"
            )
        self._grid_side[grid_path] = side

    def _note(
        self, entry_kind: "LogEntryKind", message: str, *, step: StepRef | None = None
    ) -> None:
        """Append one run-log lifecycle entry (no-op without a sink — the
        safety pass's frozen engine and hostless test engines; the buffer
        is runtime-only display state, never system state)."""
        if self._run_log is not None:
            self._run_log.note(entry_kind, message, step=step)

    async def log(self, message: str) -> None:
        """The outer-level task ``log()`` gateway (`runner_api.
        RunCapabilityProto.log`): a run-log ``log`` entry with no step
        identity. Async to match the wire method's shape; records
        nothing and is silent under the safety pass (discarded sink)."""
        self._note("log", message)

    async def _record_observation(self, subject: Subject, kind: int, fingerprint: FxtrID) -> None:
        """Durably record one observation, then mirror it in memory.

        Order matters: the db row commits first, so a crash between the two
        can only lose the (rebuildable) memory copy, never the durable one.
        Precondition: ``fingerprint``'s entity is already persisted (every
        caller passes an id it just read from, or wrote through, the
        session).

        Args:
            subject: The observed location (path, coords, output; '' forms).
            kind: An ``OBS_KIND_*`` constant.
            fingerprint: The observed value/config id.
        """
        version = await self._session.intern_version(*subject, fingerprint)
        await self._session.record_observation(self._exec_id, version, kind)
        self._record[subject] = (kind, fingerprint)

    async def _record_assertion(self, grid_path: str, config_id: FxtrID) -> None:
        """Record a would-create claim: this open ran through write
        authority, so a replay of this generation would create the grid
        if missing (grounding's ``asserted_grid``).

        Monotone per generation: idempotent inserts that accumulate
        across the generation's opens — record hits included (a resumed
        run's open through a different-authority chain may add the claim
        an earlier open didn't). A frozen engine only collects in memory:
        the safety pass must stay pure compute, and it compares
        :attr:`assertions` against the stored set instead.
        """
        self.assertions.add((grid_path, config_id))
        if not self._frozen:
            await self._session.record_grid_assertion(self._exec_id, grid_path, config_id)

    def _values_scope(self) -> FxtrScope:
        """The scope carrying the current call's incoming values.

        Engine public methods are invoked either directly by a holder
        (scope discipline's user-boundary case: the active scope
        interprets values received from the caller) or from inside a
        ``RunCapabilityImpl`` service-method dispatch (the documented
        service-method case: the framework decoded the wire args into the
        active dispatch scope; the impl is pure forwarding, so the scope
        it would have captured is still active here). Both cases sanction
        capturing the active scope at entry; it is then threaded
        *explicitly* through everything below (see
        ``v2/notes/scope_discipline.md``).
        """
        return get_active_scope()

    @staticmethod
    def _value_id(value: FxtrID | Entity | EntityRef, scope: FxtrScope) -> FxtrID:
        """The content-addressed id of a value, without persisting it.

        ``Entity.store(scope=...)`` is enough to learn the id (content
        addressing); pinning into the entity store happens later via the
        session's combined helpers.

        Args:
            value: The value to identify.
            scope: Where to stage an ``Entity`` value (the caller's
                :meth:`_values_scope`).
        """
        if isinstance(value, Entity):
            return value.store(scope=scope).entity_id
        return value.entity_id if isinstance(value, EntityRef) else value

    # -- reads --------------------------------------------------------------------

    async def _read(self, sel: ViewSel, *, kinds: tuple[str, ...]) -> FxtrID:
        """The shared read path: record-check, then live read + record.

        A hit serves the recorded id without touching live cell state
        (unless the hit is this generation's own write — reading that back
        is a read-xor-write violation). A miss claims the grid's read side,
        reads live (demanding task targets through
        :meth:`_task_output_value`), and records a ``cell_value``
        observation. Either way the served id is re-attached from the
        entity store first (`state_store.WorkflowSession.ensure_loadable`)
        — on a resumed session nothing else has tracked it, and the service
        return can only cover tracked nodes.

        Args:
            sel: A single-cell selection.
            kinds: The grid kinds this entry point accepts.

        Returns:
            The cell's value id (data), handle key (handle), or output id
            (task).

        Raises:
            ViewAxisError: Wrong grid kind, open axes, or a task read
                without an output selection.
            ReadWriteExclusionError: The generation wrote this grid.
            CellFaultError: The target is missing / not servable.
        """
        spec = self._registry.sliced(sel)
        # Coord grants have no readable storage (their value rides the grant;
        # reading one is pure) — no effectful op accepts their tokens.
        if isinstance(spec, CoordViewSpec) or spec.kind not in kinds:
            raise ViewAxisError(f"cannot read a {spec.kind!r} view this way")
        # The sliced spec's output is the authority: it carries the selection
        # whether it arrived via the wire (task-side TaskView.output()) or
        # was pre-selected by the rule binding (a data-view shadow whose
        # true spec is an output-selected task slice).
        output: str | None = None
        if isinstance(spec, TaskViewSpec):
            if spec.output is None:
                raise ViewAxisError("task reads require an output selection")
            output = spec.output
        grid_path, coords = self._resolve_cell(spec)
        subject = (grid_path, coords, output or "")
        self.visited.add(subject)
        hit = self._record.get(subject)
        if hit is not None:
            if hit[0] == OBS_KIND_WROTE_CELL:
                raise ReadWriteExclusionError(
                    f"generation wrote {grid_path}{coords}; reading it back is vacuous"
                )
            # Replay progress on a resume rides the run log (the safety
            # pass has no sink, so its hits stay silent).
            self._note("read_cell", f"read {grid_path}{coords} (replay hit)")
            # Re-attach before serving: a resumed run's hits come from the
            # state db, and the id must be tracked (not just durably
            # present) for the service return to cover it — see
            # `WorkflowSession.ensure_loadable`.
            return await self._session.ensure_loadable(hit[1])
        self._guard_frontier(subject)
        self._mark(grid_path, "read")
        # Noted at commitment, not resolution: a demand may execute a long
        # upstream cascade, and the run log should show what is being
        # waited on.
        if isinstance(spec, TaskViewSpec):
            self._note("demand", f"demand {grid_path}{coords} output {output!r}")
        else:
            self._note("read_cell", f"read {grid_path}{coords}")
        # The request commitment (before going live): the demand fact is
        # durable even if this request never resolves — grounding derives
        # a running cascade's wantedness from these rows, and the safety
        # pass compares them against the resolved record at completion.
        await self._session.record_request_edge(
            self._exec_id,
            grid_path,
            coords,
            REQ_KIND_TASK_OUTPUT if isinstance(spec, TaskViewSpec) else REQ_KIND_DATA,
        )
        if isinstance(spec, TaskViewSpec):
            value = await self._task_output_value(grid_path, coords, output or "")
        else:
            # The grounding read gate (when wired): serve-or-fail from one
            # snapshot — the executor's hook carries the policy; a gate
            # fault poisons this generation like any not-ready dependency.
            if self._read_gate is not None:
                found = await self._read_gate(grid_path, coords)
            else:
                found = await self._session.read_cell(grid_path, coords)
            if found is None:
                raise CellFaultError(f"no value at {grid_path}{coords}")
            value = found
        await self._record_observation(subject, OBS_KIND_CELL_VALUE, value)
        # Same re-attach as the hit path: read_cell / the demand hook return
        # state-db-recorded ids, tracked in-process only by residue.
        return await self._session.ensure_loadable(value)

    async def _task_output_value(self, grid_path: str, coords: str, output: str) -> FxtrID:
        """Serve one named output of a task cell (a record-miss read).

        *Every* miss-read of a task output goes through the demand hook —
        including already-complete targets — and serves from the validated
        results the hook returns: the freshness invariant (a new
        observation never sees a non-fresh cell) plus the value-pinning
        contract (check and read are one snapshot fact) both live in the
        hook; the engine never re-reads live state after the check.

        Args:
            grid_path: The task grid's key-path text.
            coords: The cell's flattened-coords text.
            output: The declared output name (never '' — task reads require
                an output selection).

        Returns:
            The result id for ``output``.

        Raises:
            CellFaultError: A hook-raised fault, or a missing output.
        """
        results = await self._demand_hook(grid_path, coords)
        if output not in results:
            raise CellFaultError(f"task cell {grid_path}{coords} has no output {output!r}")
        return results[output]

    def _check_output_kind(self, sel: ViewSel, *, frame: bool) -> "FrameSchema | None":
        """Enforce frame-vs-entity target kind for the two read ops.

        Frame ids never surface as entity values (and vice versa), so an
        entity read of a ``FrameOutput`` — or a frame read of anything
        without a declared frame schema — is rejected *before* the shared
        read path records anything.

        Args:
            sel: The op's selection.
            frame: Which read op is asking.

        Returns:
            The declared frame schema when ``frame`` (never ``None``).

        Raises:
            ViewAxisError: The selection's value kind does not match.
        """
        schema = self._registry.frame_schema(sel)
        if frame and schema is None:
            raise ViewAxisError(
                "selection does not address a frame value (a dataframe cell"
                " or a declared FrameOutput)"
            )
        if not frame and schema is not None:
            raise ViewAxisError(
                "selection addresses a frame value; read it as a frame"
                " (read_table / read_cell_frame)"
            )
        return schema

    @_effectful_op
    async def read_cell_entity(self, sel: ViewSel) -> FxtrID:
        """Read one entity cell (data grid or a task grid's entity output).

        See :meth:`_read` for the shared contract; wire contract in
        `runner_api.RunCapabilityProto.read_cell_entity`.
        """
        self._check_output_kind(sel, frame=False)
        return await self._read(sel, kinds=("data", "task"))

    @_effectful_op
    async def read_cell_frame(self, sel: ViewSel) -> bytes:
        """Read one dataframe cell (or a task grid's frame output).

        See :meth:`_read` for the shared contract (the observation
        fingerprint is the canonical frame entity id) and
        `runner_api.RunCapabilityProto.read_cell_frame` for the wire
        contract; the id is materialized to arrow IPC bytes through the
        session's projection and never crosses the wire.
        """
        # Kind-check before _read: the shared path would otherwise record
        # an observation of an entity value this op must not serve.
        self._check_output_kind(sel, frame=True)
        value = await self._read(sel, kinds=("dataframe", "task"))
        return await self._session.frame_bytes(value)

    @_effectful_op
    async def gather(self, sel: ViewSel, keys: bytes) -> bytes:
        """Batched frame read: per-cell record-check/demand, one stream out.

        Wire contract: `runner_api.RunCapabilityProto.gather`. One op (one
        request obligation), decomposing into ordinary per-cell state: the
        record-check applies per distinct addressed cell (hits serve the
        recorded fingerprint; a frozen engine replays a recorded gather as
        pure hits), every miss's request edge is committed *before* any
        resolution (request = observation, batch form), and misses resolve
        **concurrently** — the executor's in-flight map dedups shared
        upstream demands; nothing recorded depends on resolution order.

        Failure semantics are per request, exactly as if the cells were k
        parallel single reads: each failing cell records *its own*
        failure row at raise time, classified from its own typed
        exception (so the generation's terminal status derives from the
        recorded set — error-class outranks fault — with no
        exception-picking anywhere). The op then raises one synthesized
        :class:`~fxtr_experiment.errors.GatherFailedError` naming every
        failed cell — a signal for the already-recorded causes, itself
        never persisted (`_record_run_failure` skips it). Observations
        recorded before a failure stand, and a resumed identical gather
        replays them as hits. Cancellation stays op-grain: an externally
        cancelled gather records the op wrapper's single
        ``RequestAbortedError`` (the per-cell asked-but-unanswered facts
        are already durable as request edges).
        """
        schema = self._check_output_kind(sel, frame=True)
        assert schema is not None  # frame=True never returns None
        base = self._registry.sliced(sel)
        if isinstance(base, CoordViewSpec) or base.kind not in ("dataframe", "task"):
            raise ViewAxisError(f"cannot gather a {base.kind!r} view")
        output = ""
        if isinstance(base, TaskViewSpec):
            assert base.output is not None  # frame schema ⇒ output selected
            output = base.output
        grant_axes = self._registry.spec_of(sel.token).axes
        open_positions = [p for p in range(len(grant_axes)) if p not in sel.bound]
        kinds: tuple[AxisKind, ...] = tuple(grant_axes[p].kind for p in open_positions)
        # Lazy import: the sole eager-polars module, first frame op only.
        from fxtr_experiment_task import tables

        # Resolve every distinct key to its subject (kind/value validation
        # happens in decode_key_table and bind_keeps; per-key slicing is
        # exactly a single read's resolution with the key's bindings merged).
        subjects: dict[Subject, tuple[CoordValue, ...]] = {}
        for key in tables.decode_key_table(keys, kinds):
            bound = dict(sel.bound) | dict(zip(open_positions, key))
            spec = self._registry.sliced(
                ViewSel(token=sel.token, bound=frozendict(bound), output=sel.output)
            )
            assert not isinstance(spec, CoordViewSpec)  # base kind checked above
            grid_path, coords = self._resolve_cell(spec)
            subjects[(grid_path, coords, output)] = key
        if not subjects:
            # Zero addressed cells: a pure no-op (no claim, no records),
            # serving the empty, correctly-typed stream.
            return tables.frame_to_ipc(tables.stitch_gather(kinds, schema, ()))
        self._mark(next(iter(subjects))[0], "read")  # one grid per gather
        resolved: dict[Subject, FxtrID] = {}
        misses: list[Subject] = []
        for subject in sorted(subjects):
            self.visited.add(subject)
            hit = self._record.get(subject)
            if hit is not None:
                if hit[0] == OBS_KIND_WROTE_CELL:
                    raise ReadWriteExclusionError(
                        f"generation wrote {subject[0]}{subject[1]}; reading it" " back is vacuous"
                    )
                resolved[subject] = hit[1]
            else:
                self._guard_frontier(subject)
                misses.append(subject)
        self._note(
            "read_cell",
            f"gather {len(subjects)} cells of {next(iter(subjects))[0]}"
            f" ({len(resolved)} replay hits)",
        )
        # The batch commitment: every miss's request edge is durable before
        # anything resolves — grounding sees the whole ask, and an aborted
        # batch's unanswered requests are the normal interrupted shape.
        req_kind = REQ_KIND_TASK_OUTPUT if output else REQ_KIND_DATA
        for grid_path, coords, _ in misses:
            await self._session.record_request_edge(self._exec_id, grid_path, coords, req_kind)

        failures: list[tuple[Subject, BaseException]] = []

        async def resolve_one(subject: Subject) -> None:
            grid_path, coords, out = subject
            try:
                if out:
                    self._note("demand", f"demand {grid_path}{coords} output {out!r}")
                    value = await self._task_output_value(grid_path, coords, out)
                else:
                    self._note("read_cell", f"read {grid_path}{coords}")
                    if self._read_gate is not None:
                        found = await self._read_gate(grid_path, coords)
                    else:
                        found = await self._session.read_cell(grid_path, coords)
                    if found is None:
                        raise CellFaultError(f"no value at {grid_path}{coords}")
                    value = found
            except Exception as exc:
                # Per-request failure semantics: this cell's failure is its
                # own recorded fact, classified from its own typed
                # exception — exactly what a standalone read's op wrapper
                # would persist. (Cancellation is not caught: sibling/
                # external cancellation records nothing per cell; the op
                # wrapper's abort arm owns the op-grain event.)
                await self._record_run_failure(exc)
                failures.append((subject, exc))
                raise
            await self._record_observation(subject, OBS_KIND_CELL_VALUE, value)
            resolved[subject] = value

        if misses:
            try:
                async with anyio.create_task_group() as tg:
                    for subject in misses:
                        tg.start_soon(resolve_one, subject)
            except ExceptionGroup as eg:
                # An ExceptionGroup's leaves are all Exceptions — i.e.
                # exactly what resolve_one collected into `failures` (the
                # constructor auto-specializes: any cancellation or other
                # bare-BaseException leaf makes the group a
                # BaseExceptionGroup, which this deliberately does NOT
                # catch — such a group is never ours to translate, and
                # cancellation in particular is never absorbed here; the
                # op wrapper's arms own op-grain events).
                raise _gather_failed(failures) from eg
        # Stitch in sorted-coords order (one grid, one output: sorting the
        # subjects is sorting the coords), each cell via the projection.
        parts = []
        for subject in sorted(resolved):
            data = await self._session.frame_bytes(resolved[subject])
            parts.append((subjects[subject], tables.ipc_to_frame(data)))
        return tables.frame_to_ipc(tables.stitch_gather(kinds, schema, parts))

    @_effectful_op
    async def read_handle_key(self, sel: ViewSel) -> FxtrID:
        """Read one handle cell's key (the identity observation that grants
        connect authority — see ``notes/design_overview.md``, authority and
        trust).

        See :meth:`_read` for the shared contract. Every key read here —
        live or replayed hit — joins this run's connect-authority set
        (program order guarantees the read replays before any step that
        connects the key).
        """
        key = await self._read(sel, kinds=("handle",))
        self._handle_keys_read.add(key)
        return key

    # -- writes --------------------------------------------------------------------

    @_effectful_op
    async def write_cell_entity(self, sel: ViewSel, value: FxtrID | Entity | EntityRef) -> None:
        """Write one entity cell (idempotent on identical id).

        Hit rules: this generation's identical prior write is a no-op (the
        durable write already happened — replay); a prior write of a
        *different* value is ``NondeterminismError``; a prior *read* of
        this cell is ``ReadWriteExclusionError``. On a miss the grid's
        write side is claimed, a differing live value is an ordinary
        ``WriteConflictError``, and success records the ``wrote_cell``
        observation (persist-then-record via the session) — which doubles
        as the generation's write claim on the value.

        Args:
            sel: A single-cell selection on a data grant (grant mode must
                be write — the authority check; the view layer separately
                enforces the *effective* mode).
            value: The value; entities/refs are persisted by the session
                helper before their id is recorded.

        Raises:
            ViewAxisError: Not a data grant, read-mode grant, open axes.
            NondeterminismError: Conflicts with this generation's record.
            ReadWriteExclusionError: The generation read this grid.
            WriteConflictError: A different live value is present.
        """
        values_scope = self._values_scope()  # boundary capture; see its doc
        spec = self._registry.sliced(sel)
        if spec.kind != "data":
            raise ViewAxisError(f"cannot write a {spec.kind!r} view")
        if spec.mode != "write":
            raise ViewAxisError("view is not writable (read mode)")
        value_id = self._value_id(value, values_scope)
        await self._write_cell(spec, value, value_id, values_scope)

    @_effectful_op
    async def write_cell_frame(self, sel: ViewSel, frame: bytes) -> None:
        """Write one dataframe cell (idempotent on multiset equality).

        The frame's identity is minted here, host-side, under the grid's
        *declared* schema (`frame_encoding.canonical_frame_from_ipc`); the
        record-check/conflict/claim logic is then :meth:`write_cell_entity`'s
        exactly, comparing canonical frame entity ids — so a replay hit
        canonicalizes the incoming bytes just to compare (accepted v0
        cost), and multiset equality is an id comparison. Wire contract:
        `runner_api.RunCapabilityProto.write_cell_frame`.

        Args:
            sel: A single-cell selection on a write-mode dataframe grant.
            frame: Arrow IPC stream bytes (any row order).

        Raises:
            ViewAxisError: Not a dataframe grant, read mode, open axes, or
                the frame fails validation against the declared schema.
            NondeterminismError: Conflicts with this generation's record.
            ReadWriteExclusionError: The generation read this grid.
            WriteConflictError: A different live multiset is present.
        """
        values_scope = self._values_scope()  # boundary capture; see its doc
        spec = self._registry.sliced(sel)
        if spec.kind != "dataframe":
            raise ViewAxisError(f"cannot write a {spec.kind!r} view as a frame")
        if spec.mode != "write":
            raise ViewAxisError("view is not writable (read mode)")
        schema = self._registry.frame_schema(sel)
        if schema is None:
            raise InternalRunnerError("dataframe grant minted without its declared schema")
        # Lazy import: reaches the sole eager-polars module (`tables`).
        from fxtr_experiment.frame_encoding import canonical_frame_from_ipc

        try:
            entity = canonical_frame_from_ipc(frame, schema)
        except EncodingError as exc:
            raise ViewAxisError(f"frame rejected by the declared schema: {exc}") from exc
        value_id = self._value_id(entity, values_scope)
        await self._write_cell(spec, entity, value_id, values_scope)

    async def _write_cell(
        self,
        spec: StoredViewSpec,
        value: FxtrID | Entity | EntityRef,
        value_id: FxtrID,
        values_scope: FxtrScope,
    ) -> None:
        """The shared write tail: record-check, conflict, persist, claim.

        Kind/mode/identity work is the callers' (entity vs frame); from the
        resolved cell on, the two writes are one op. ``value_id`` is
        ``value``'s content id (already computed via :meth:`_value_id`, so
        an ``Entity`` is staged in ``values_scope``).
        """
        grid_path, coords = self._resolve_cell(spec)
        subject = (grid_path, coords, "")
        self.visited.add(subject)
        hit = self._record.get(subject)
        if hit is not None:
            kind, fingerprint = hit
            if kind != OBS_KIND_WROTE_CELL:
                raise ReadWriteExclusionError(
                    f"generation read {grid_path}{coords}; writing it now is a "
                    "latent self-conflict"
                )
            if fingerprint != value_id:
                raise NondeterminismError(
                    f"generation previously wrote a different value to " f"{grid_path}{coords}"
                )
            self._note("wrote_cell", f"wrote {grid_path}{coords} (replay hit)")
            return  # replay hit: the write already happened durably
        self._guard_frontier(subject)
        self._mark(grid_path, "write")
        existing = await self._session.read_cell(grid_path, coords)
        if existing is not None and existing != value_id:
            raise WriteConflictError(f"cell {grid_path}{coords} already holds a different value")
        await self._session.write_cell(grid_path, coords, value, from_scope=values_scope)
        # The wrote_cell observation IS the write claim (grounding's
        # `wrote`): no separate sourcing row — claims are read off the
        # record, and expire with the generation exactly like it.
        await self._record_observation(subject, OBS_KIND_WROTE_CELL, value_id)
        self._note("wrote_cell", f"wrote {grid_path}{coords}")

    # -- opens ------------------------------------------------------------------------

    async def _open(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        kind: GridKind,
        config: GridConfig,
        mode: 'SliceMode | Literal["inherit"]',
        *,
        schema: FrameSchema | None = None,
        outputs: "frozendict[str, OutputSpec] | None" = None,
        premade: ViewGrant | None = None,
    ) -> ViewGrant:
        """The shared open path: create-or-attach on canonical config.

        Order: derive the child spec (validates subtree-ness, the
        full-frame invariant, and own-axis name uniqueness) → compute the
        canonical config id (the ``grid_config`` fingerprint) → record-check
        the grid subject (hit with a different config =
        ``NondeterminismError``) → on a miss, attach if the live grid's
        config matches, else create (requiring create rights; task grids
        also populate the reverse rule-reference index) → record the
        ``grid_config`` observation → on hit and miss alike, record the
        would-create claim when the open ran through write authority.

        Args:
            subtree: The subtree (or internals) grant to open under.
            key: The child key.
            axes: The child's own axes (official names).
            kind: The child grid kind.
            config: The child's canonical config (its entity id is the
                fingerprint compared everywhere).
            mode: Access mode of the returned grant — an *internal*
                parameter, never caller-chosen (opens follow the chain).
                ``"inherit"`` resolves to the subtree grant's own mode
                (the public opens); the kind-keyed callers fix it
                (``open_handle``: read, attach-only; ``open_task``: task
                grants are read-shaped; ``open_checkpoint``: write —
                checkpoints are write-sites, so a forwarded read-mode
                internals grant is rejected early by ``open_child``).
            schema: Passed through to the grant for dataframe kinds.
            outputs: Passed through to the grant for task kinds.
            premade: A grant already minted by the caller (``open_task``
                mints via the registry translation); returned as-is instead
                of minting a second one.

        Returns:
            The child grant.

        Raises:
            NondeterminismError: Hit with a different config fingerprint.
            GridConfigMismatchError: Live grid with a different config, or
                a checkpoint open outside the caller's internals.
            CellFaultError: A handle grid that does not exist (attach-only:
                tasks never create handle grids).
            ViewAxisError: No create rights (read-mode subtree), or invalid
                child derivation.
        """
        # Boundary capture (see _values_scope): _open is the immediate
        # funnel of the public open_* entry points, so the active scope
        # here is still the caller's/dispatch scope carrying the call.
        values_scope = self._values_scope()
        subtree_spec = self._registry.sliced(subtree)
        if not isinstance(subtree_spec, SubtreeViewSpec):
            raise ViewAxisError(f"cannot open children under a {subtree_spec.kind!r} view")
        if mode == "inherit":
            mode = subtree_spec.mode
        child_spec = open_child(subtree_spec, key, axes, kind, mode=mode)  # validates
        grid_path = encode_key_path(tuple(step.key for step in child_spec.path))
        config_id = store_config(config, scope=values_scope)
        subject = (grid_path, "", "")
        self.visited.add(subject)
        hit = self._record.get(subject)
        if hit is not None:
            if hit[1] != config_id:
                raise NondeterminismError(
                    f"generation previously opened {grid_path} with a different config"
                )
        else:
            self._guard_frontier(subject)
            existing = await self._session.get_grid(grid_path)
            if existing is not None:
                if existing[1] != config_id:
                    raise GridConfigMismatchError(
                        f"{grid_path} exists with a different canonical config"
                    )
            else:
                if kind == "handle":
                    raise CellFaultError(
                        f"handle grid {grid_path} does not exist (tasks never "
                        "create handle grids; create it at the top level)"
                    )
                if kind == "checkpoint" and not subtree_spec.internals:
                    raise GridConfigMismatchError(
                        "checkpoint grids only open within the owning task's internals"
                    )
                if subtree_spec.mode != "write":
                    raise ViewAxisError("subtree view does not carry create access")
                refs: tuple[str, ...] = ()
                if isinstance(config, TaskGridConfig):
                    refs = tuple(encode_key_path(p) for p in rule_reference_paths(config.rule))
                await self._session.create_grid(
                    grid_path,
                    kind,
                    GridConfigEntity(config=config),
                    rule_refs=refs,
                    from_scope=values_scope,
                )
            await self._record_observation(subject, OBS_KIND_GRID_CONFIG, config_id)
        if subtree_spec.mode == "write" and kind != "handle":
            # A would-create claim, on hit and miss alike (grounding's
            # asserted_grid): this open ran through write authority, so a
            # replay would create the grid if missing — handle opens are
            # attach-only (tasks never mint authority) and never claim.
            await self._record_assertion(grid_path, config_id)
        if premade is not None:
            return premade
        return self._registry.mint(child_spec, schema=schema, outputs=outputs)

    @_effectful_op
    async def open_data(self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]) -> ViewGrant:
        """Open (create-or-attach) a data grid. Opens carry no mode: the
        grant follows the chain's authority. See :meth:`_open`."""
        return await self._open(subtree, key, axes, "data", DataGridConfig(axes=axes), "inherit")

    @_effectful_op
    async def open_dataframe(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        schema: FrameSchema,
    ) -> ViewGrant:
        """Open (create-or-attach) a dataframe grid. See :meth:`_open`."""
        return await self._open(
            subtree,
            key,
            axes,
            "dataframe",
            DataFrameGridConfig(axes=axes, schema=schema),
            "inherit",
            schema=schema,
        )

    @_effectful_op
    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Attach to an existing handle grid (read-only; never creates —
        tasks cannot mint authority). See :meth:`_open`."""
        return await self._open(subtree, key, axes, "handle", HandleGridConfig(axes=axes), "read")

    @_effectful_op
    async def open_subtree(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Open (create-or-attach) a nested subtree grid. The child inherits
        the grant's mode: a creation site under a write grant, a read-only
        browse one level deeper under a read grant. See :meth:`_open`."""
        return await self._open(
            subtree, key, axes, "subtree", SubtreeGridConfig(axes=axes), "inherit"
        )

    @_effectful_op
    async def open_checkpoint(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Open a checkpoint grid — only under an internals grant
        (ownership is structural in the path). See :meth:`_open`."""
        return await self._open(
            subtree, key, axes, "checkpoint", CheckpointGridConfig(axes=axes), "write"
        )

    @_effectful_op
    async def open_task(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        task_fn: WiredArg,
        args: "Mapping[str, WiredArg | WiredCoord]",
        outputs: "frozendict[str, OutputSpec]",
    ) -> ViewGrant:
        """Open (create-or-attach) a task grid, canonicalizing the wiring.

        The registry translation runs the canonicalizer first (rejecting
        bad wiring / batch-invariance violations before any storage is
        touched); the resulting canonical config then goes through the
        shared create-or-attach path, so regularity conflicts surface as
        config mismatches exactly like any other open.

        Args:
            subtree: The subtree grant to create into.
            key: The child key.
            axes: The task grid's own axes (official names).
            task_fn: Wired handle selection (fully consumed).
            args: Named wired args (see `runner_api.WiredArg`).
            outputs: The declared outputs (explicit on the wire).

        Returns:
            The task grant (minted by the registry translation).
        """
        config, grant = self._registry.open_task(subtree, key, axes, task_fn, args, outputs)
        return await self._open(subtree, key, axes, "task", config, "read", premade=grant)

    # -- steps (chunk 7) -----------------------------------------------------------

    @_effectful_op
    async def run_step(
        self,
        checkpoint: ViewSel,
        step: ServiceRef[UnknownService],
        args: "Mapping[str, FxtrServiceData]",
    ) -> "FxtrServiceData":
        """Run a step durably into one checkpoint cell.

        Wire contract: `runner_api.RunCapabilityProto.run_step`. The
        recorded invocation is ``(step service type id, args fingerprint)``
        — the args are entity-encoded (`encoding.store_step_payload`,
        which also pins the closures of any ids they carry). One call per
        checkpoint cell per run (:class:`~fxtr_experiment.errors.
        DuplicateStepError`); a hit with a matching invocation serves the
        stored result, a mismatched hit is nondeterminism; a miss runs the
        step under the :class:`StepHost`'s authority — per-step scope,
        mounted :class:`StepCapabilityImpl`, persist-inside-scope, record
        — and both paths serve the *canonical payload round-trip*, so a
        replayed result is indistinguishable from a first execution.

        Raises:
            ViewAxisError: Not a writable checkpoint grant / open axes.
            DuplicateStepError: Second call for this cell in this run.
            StepPayloadError: Non-durable args or result.
            NondeterminismError: Hit with a different invocation.
            CheckpointCorruptionError: A live entry bound to a foreign
                generation.
            StepRaisedError: The step service raised (errored-class).
            StepInvocationFailedError: A ``connect`` failure poisoned the
                invocation — the failure was recorded at its raise site
                (``HandleUnboundError`` → fault/interrupted;
                ``ConnectAuthorityError`` → errored) and the invocation
                fails even if step code caught the error (no checkpoint
                entry is written); this signal names the cause.
            InternalRunnerError: A miss on a hostless engine.
        """
        values_scope = self._values_scope()  # boundary capture; see its doc
        spec = self._registry.sliced(checkpoint)
        if spec.kind != "checkpoint":
            raise ViewAxisError(f"run_step requires a checkpoint grant, got {spec.kind!r}")
        if spec.mode != "write":
            raise ViewAxisError("checkpoint grant is not writable (read mode)")
        grid_path, coords = self._resolve_cell(spec)
        subject = (grid_path, coords, "")
        if subject in self._steps_called:
            raise DuplicateStepError(
                f"run_step called twice for checkpoint {grid_path}{coords} in one run"
                " (one call per cell per run; dedup belongs in task logic)"
            )
        self._steps_called.add(subject)
        self.visited.add(subject)
        step_type_id = step.service_type_id
        args_id = store_step_payload(dict(args), scope=values_scope)
        entry = self._checkpoints.get(subject)
        if entry is not None:
            recorded_type, recorded_args, recorded_value = entry
            if recorded_type != step_type_id or recorded_args != args_id:
                raise NondeterminismError(
                    f"checkpoint {grid_path}{coords} was recorded with a different"
                    " invocation (step identity or args changed)"
                )
            self._note(
                "step_hit",
                f"step {grid_path}{coords} served from checkpoint",
                step=StepRef(grid_path, coords),
            )
            return await self._session.load_step_payload(recorded_value)
        self._guard_frontier(subject)
        if self._step_host is None:
            raise InternalRunnerError("run_step miss on an engine constructed without a StepHost")
        live = await self._session.get_checkpoint(grid_path, coords)
        if live is not None:
            # Our own entries were loaded into memory; anything live here
            # belongs to a foreign generation (turnover should have swept it).
            raise CheckpointCorruptionError(
                f"checkpoint {grid_path}{coords} holds an entry bound to a" " foreign generation"
            )
        async with FxtrScope(
            name="wf3-step", isolated=True, include=[self._step_host.scope]
        ) as step_scope:
            # The step ref arrived through this op's dispatch scope; bridge
            # it so the step scope can resolve it (dest nested inside the
            # in-flight source call — the sanctioned arrangement).
            await step_scope.bridge_service_from_scope(
                step, values_scope, keepalive_via="independent_lease"
            )
            step_ref = StepRef(grid_path, coords)
            capability = StepCapabilityImpl(self, self._step_host, step_ref=step_ref)
            cap_ref = await step_scope.mount(capability)
            proxy = await step.replace_service_type(WorkflowStepFnProto).resolve(step_scope)
            self._note("step_started", f"step {grid_path}{coords} started", step=step_ref)
            try:
                result = await proxy.run_step(cap_ref, dict(args))
            except Exception as exc:
                # The one step outcome with no checkpoint row: surface it
                # on the run-log so the drawer can show the step errored
                # (completion is signaled by the checkpoint entry instead).
                self._note("step_errored", f"step {grid_path}{coords} raised: {exc}", step=step_ref)
                if capability.poisoned is not None:
                    # The poisoning was registered inside connect (raise
                    # time); this is the *signal* to the demanding task —
                    # a fresh exception, chained from what escaped the
                    # step for propagation context.
                    raise _invocation_failed(grid_path, coords, capability.poisoned) from exc
                raise StepRaisedError(f"step at {grid_path}{coords} raised: {exc}") from exc
            if capability.poisoned is not None:
                # The step caught (or ignored) its connect failure and
                # returned anyway: the poisoning stands — no checkpoint
                # entry is written; the signal chains from the cause.
                self._note(
                    "step_errored",
                    f"step {grid_path}{coords} failed: {capability.poisoned}",
                    step=step_ref,
                )
                raise _invocation_failed(
                    grid_path, coords, capability.poisoned
                ) from capability.poisoned
            value_frozen = service_data_to_frozen_cbor(result)
            value_id = StepPayloadEntity(payload=value_frozen).store(scope=step_scope).entity_id
            # Persist-then-record, each payload from the scope that holds
            # its closure (args: the dispatch; value: the step's return
            # arena — which dies with this scope, hence inside it). The
            # step's type entity is a recorded id too (the extraction
            # contract): define_type stored it into GLOBAL_SCOPE, an
            # ancestor of the dispatch chain, so the dispatch scope
            # covers it.
            await self._session.persist(args_id, from_scope=values_scope)
            await self._session.persist(value_id, from_scope=step_scope)
            await self._session.persist(step_type_id, from_scope=values_scope)
            await self._session.record_checkpoint(
                grid_path, coords, self._exec_id, step_type_id, args_id, value_id
            )
        self._checkpoints[subject] = (step_type_id, args_id, value_id)
        return frozen_cbor_to_service_data(value_frozen)


STEP_CAPABILITY_TYPE_ID = define_type(WORKFLOWS3_NS, "step_capability")


class StepCapabilityImpl(TypedServiceImpl[StepCapabilityProto]):
    """One step invocation's gateway (contract:
    `runner_api.StepCapabilityProto`), mounted in the per-step scope.

    Carries the invocation's **poisoned flag**: a failed ``connect``
    records its failure at raise time *and* marks the invocation, and the
    ``run_step`` body consults the flag after the step returns — so a
    step that catches (or ignores) the failure and returns a value still
    fails the invocation and writes no checkpoint entry (the poisoning
    contract, applied to the one workflow op available inside steps).
    """

    def __init__(self, engine: OpsEngine, host: StepHost, *, step_ref: StepRef) -> None:
        """Bind to the invoking engine (record access) and its host.

        Args:
            engine: The invoking engine.
            host: Its step host.
            step_ref: This invocation's identity (`watch_api.StepRef` —
                the checkpoint cell) — stamps the run-log entries
                ``log()`` produces.
        """
        super().__init__()
        self._engine = engine
        self._host = host
        self._step_ref = step_ref
        self.poisoned: WorkflowError | None = None

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return StepCapabilityProto

    def _service_type_id(self) -> FxtrID:
        """The step-capability service type id."""
        return STEP_CAPABILITY_TYPE_ID

    @service_method
    async def connect(self, handle: FxtrID) -> ServiceRef[UnknownService]:
        """Exchange a previously-read handle key for a live service.

        Contract: `runner_api.StepCapabilityProto.connect`. Authority
        follows the record — the key must be in this run's handle-read set
        (reads replay in program order before any step that uses them) —
        and resolution goes through the host's bindings. Failures are
        persisted at raise time and poison this invocation (see the class
        docstring).

        Raises:
            ConnectAuthorityError: The generation never read this key from
                a handle grant (errored-class).
            HandleUnboundError: Authority is fine but no live binding
                exists in this runner (fault-class → interrupted; bind and
                re-demand).
        """
        # Active scope: the per-call dispatch scope (the documented
        # service-method exception) — where the spawned ref must land so
        # the framework can chain-bridge it back to the step.
        call_scope = get_active_scope()
        try:
            if handle not in self._engine._handle_keys_read:
                raise ConnectAuthorityError(
                    f"key {handle.id_string()} was not read from a handle grant by"
                    " this generation (authority never flows through data)"
                )
            return await self._host.bindings.spawn(handle, into_scope=call_scope)
        except WorkflowError as exc:
            await self._engine._record_run_failure(exc)  # raise-time persistence
            if self.poisoned is None:
                self.poisoned = exc
            raise

    @service_method
    async def log(self, message: str) -> None:
        """Contract on `runner_api.StepCapabilityProto.log`: append one
        entry to the cell's runtime-only run log (`run_log.py` — never
        persisted, not load-bearing), stamped with this invocation's
        step identity. Silent under the safety pass and on record-hit
        replays (hits never invoke step fns; the frozen engine has no
        sink)."""
        self._engine._note("log", message, step=self._step_ref)
