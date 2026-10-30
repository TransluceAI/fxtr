"""The executor: demand path + run orchestration.

:class:`Executor` drives task-cell generations per the decided execution
semantics (``notes/execution_semantics.md``). :meth:`Executor.demand` is
both the public entry point and the engine's demand hook, so transitive
demands recurse naturally: reading an absent upstream task output executes
it first, depth-first.

**The demand policy** (v0 defaults; currency is evaluated on demand via
`state_store.WorkflowSession.cell_currency` — the strict gate: a demand succeeds only
for a complete *and fresh* cell, and what it returns is that snapshot's
**validated results** — check and read are one value-pinned fact, so the
engine serves and records exactly what the gate checked, and nothing can
turn over in between):

- no generation → mint one and run it;
- ``running`` → await the in-flight run (in-process events; v0
  single-runner), then re-resolve — which re-checks currency, so a
  result that arrives stale or unsettled faults rather than being
  observed (the on-arrival check);
- **stale**, whatever the execution status → fault immediately (v0 never
  silently discards a generation's state — the user clears it);
- **unsettled**, whatever the execution status → fault, with the
  diagnosis naming the first unsettled/stale root found in the support;
- ``interrupted`` + fresh → resume: an ordinary run of the same
  generation (record hits replay to the frontier; the blocked op retries
  live);
- ``errored`` + fresh → this demand *is* the retry: one fresh run of the
  same generation, replaying the record; fault if it fails again ("retry
  once" emerges per demand);
- ``complete`` + fresh → serve.

A run that completes is currency-checked before the demand returns: v0
lets a running-but-stale generation finish (its result lands coherently,
already stale), but the demander never observes it as fresh.

**One run** (:meth:`Executor._run_once`):

1. Mint the run's grants from the stored rule
   (`spec_registry.mint_rule_grants` over a session-backed lookup). This
   is also the pre-run existence check on every grid the rule references:
   it runs *before* the generation row is touched, so a rule pointing at
   a deleted grid faults with no generation state changed.
2. Get-or-mint the generation. At first mint, the rule's ``task_fn``
   slice is resolved and the fn cell read *via the session* purely to
   populate the ``current_fn``/``executed_fn`` columns (bookkeeping, not
   an observation); a missing fn cell faults with no generation minted.
   On any later run, the last attempt's failure records are cleared
   (they are evidence about that attempt, consumed by its terminal
   status) and the status flips back to ``running``.
3. Open the per-run scope (an isolated child of the runner scope, per
   the execution-semantics scope tree) and, inside it: load the engine
   (loading *is* resume: the record and checkpoint entries become hits)
   with a `~fxtr_experiment.ops_engine.StepHost` carrying the run scope
   and the binding host — per-step scopes are isolated children of
   the run scope, and steps' ``connect`` spawns resolve through it. Then
   perform the **run
   prologue** — read the task-fn cell's key *through the engine* (the one
   recorded fn observation; replays like any other op), checked against
   the minted ``executed_fn`` as a safety net for a concurrent fn-cell
   edit between mint and prologue — then spawn the task-fn service
   through the binding host, mount the run capability, invoke the
   task fn, wait for the obligation set to drain, and persist the
   results (``record_result`` with the run scope as the pin source —
   result closures ride the return arena and die with the scope, so
   persistence must happen before it closes).
4. Derive the terminal status. The run's failure set is ground truth
   (never anything that crossed the wire): any error-class failure →
   ``errored``; only fault-class failures → ``interrupted``; an exception
   escaping with *no* recorded failure is classified by **one shared
   rule** (:meth:`Executor._record_escape`, shared with the safety
   pass) — a *runner-side* typed ``CellFaultError`` (the task-fn spawn
   found its binding missing — raised in this process, not transit)
   records fault-class → ``interrupted``, anything else → ``errored``
   (the task's own logic raised — recorded as a ``TaskRaisedError``
   failure); cancellation with no recorded failure → ``interrupted``
   (the run was cut off between ops — recorded as a fault-class
   ``RunCancelled`` failure under a narrow shield, then re-raised). A
   clean return with an empty failure set proceeds to the safety pass.
5. **The safety pass** (:meth:`Executor._safety_pass`, always-on): the
   outer level must be deterministic-given-the-record, and this is
   verified, not assumed — a full replay against a frozen engine must hit
   every recorded op, visit nothing else, and reproduce the recorded
   outputs. Only then does the generation flip to ``complete``; a
   violation records a ``NondeterminismError`` failure and flips it to
   ``errored``. Because the pass re-invokes the task fn, its *setup*
   (grant minting, the task-fn spawn/connect) shares a run's not-ready
   surface, and escapes classify by the same shared rule: a fault-class
   (``CellFaultError``) escape — e.g. the binding vanished between run
   and pass — records fault-class evidence and lands ``interrupted``
   ("couldn't finish", retryable), never mislabeled as nondeterminism.
   Consequence: every completed generation invoked its task
   fn (at least) twice — the run and the pass. Steps are *not* doubled:
   the pass replays checkpoints as hits and never invokes a step fn.

Scope discipline notes (`v2/notes/scope_discipline.md`): the executor owns
the per-run scope; every ref it handles travels with the scope it is
resolvable in (the fn ref is spawned into the run scope; the capability is
mounted there); results are persisted ``from_scope=run_scope``.
"""

from __future__ import annotations

from typing import Literal, Mapping

import anyio

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment.currency import CurrencyReport
from fxtr_experiment.encoding import decode_coords
from fxtr_experiment_task.errors import (
    CellFaultError,
    EncodingError,
    InternalRunnerError,
    TaskOutputError,
    UngroundedSubjectError,
    ViewAxisError,
)
from fxtr_experiment_task.model import (
    FrameOutput,
    GridConfig,
    OutputSpec,
    TaskGridConfig,
    TerminalStatus,
)
from fxtr_experiment.ops_engine import OpsEngine, StepHost, new_generation_id, resolve_templates
from fxtr_experiment.run_log import RunLogRegistry
from fxtr_experiment_task.runner_api import ViewSel, WorkflowTaskFnProto
from fxtr_experiment.spec_registry import SpecRegistry, mint_rule_grants
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import OBS_KIND_CELL_VALUE, REQ_KIND_DATA, REQ_KIND_TASK_OUTPUT
from fxtr_experiment.storage import FAILURE_ERROR, FAILURE_FAULT
from fxtr_experiment.bindings import BindingHost
from fxtr_experiment.task_service import RunCapabilityImpl


def _canonical_result_ids(
    results: Mapping[str, object],
    outputs: Mapping[str, OutputSpec],
    scope: FxtrScope,
) -> dict[str, FxtrID]:
    """Validate a run's returned outputs host-side; mint frame identity.

    The task-side normalization (`task_service.normalize_outputs`) is
    convenience, not authority — it ran across the wire. This is the
    host's check: the name set must match the declared outputs exactly,
    entity slots must carry an ``FxtrID``, and ``FrameOutput`` slots carry
    wire frame bytes that are canonicalized *here* under the declared
    schema (`frame_encoding.canonical_frame_from_ipc`) — identity is minted
    host-side in every path. Frame entities are staged into ``scope``
    (content addressing: ``store`` learns the id); persisting them is the
    caller's move (the results loop pins from the run scope; the safety
    pass hands a scratch scope and persists nothing).

    Raises:
        TaskOutputError: A name-set mismatch, a wrong wire type, or a
            frame payload failing validation against its declared schema.
    """
    if set(results) != set(outputs):
        raise TaskOutputError(f"returned outputs {sorted(results)} != declared {sorted(outputs)}")
    ids: dict[str, FxtrID] = {}
    for name, value in results.items():
        spec = outputs[name]
        if isinstance(spec, FrameOutput):
            if not isinstance(value, bytes):
                raise TaskOutputError(
                    f"frame output {name!r} crossed as {type(value).__name__}, not bytes"
                )
            # Lazy import: reaches the sole eager-polars module (`tables`).
            from fxtr_experiment.frame_encoding import canonical_frame_from_ipc

            try:
                entity = canonical_frame_from_ipc(value, spec.schema)
            except EncodingError as exc:
                raise TaskOutputError(
                    f"frame output {name!r} rejected by its declared schema: {exc}"
                ) from exc
            ids[name] = entity.store(scope=scope).entity_id
        else:
            if not isinstance(value, FxtrID):
                raise TaskOutputError(
                    f"output {name!r} crossed as {type(value).__name__}, not an entity id"
                )
            ids[name] = value
    return ids


class Executor:
    """Drives task-cell generations for one open workflow session.

    One executor per runner process (v0 single-runner). Holds no durable
    state of its own — everything lives in the session; the in-flight map
    only deduplicates concurrent demands within this process.

    Attributes:
        run_log: The runner's per-cell run-log buffers (`run_log.py`) —
            executor-owned because the executor owns the run lifecycle:
            each run restarts its cell's buffer and threads the sink into
            the engine (the safety pass gets none). The capability
            mount's watch engine consumes it (snapshots + the append
            listener).
        _session: The open two-file durable session.
        _bindings: The runner's binding host (`bindings.BindingHost` —
            both classes; the serving funnel). Its scope must be
            reachable from ``_runner_scope`` (they are typically the
            same scope), so spawned refs resolve in run scopes.
        _runner_scope: The runner scope (execution-semantics scope tree):
            sees the session's entity namespace; every per-run scope is an
            isolated child of exactly this scope.
        _inflight: (grid path, coords) → completion event, deduplicating
            concurrent demands of one cell.
    """

    def __init__(
        self,
        session: WorkflowSession,
        bindings: BindingHost,
        runner_scope: FxtrScope,
        *,
        grounding_policy: 'Literal["strict", "off"]' = "strict",
    ) -> None:
        """Wire the executor to its session, bindings, and runner scope.

        Args:
            session: The open two-file durable session.
            bindings: The runner's binding host (the serving funnel).
            runner_scope: The runner scope.
            grounding_policy: The grounding gate's mode
                (``notes/grounding.md``, the gate table): ``strict``
                (default) faults new observations of ungrounded subjects;
                ``off`` serves regardless (lint only). Pure policy over
                the derived verdicts — switching modes changes no
                definitions and no stored state.
        """
        self._session = session
        self._bindings = bindings
        self._runner_scope = runner_scope
        self._grounding_strict = grounding_policy == "strict"
        self._inflight: dict[tuple[str, str], anyio.Event] = {}
        self.run_log = RunLogRegistry()

    @property
    def grounding_strict(self) -> bool:
        """Whether the grounding gate is on (the ``strict`` policy) — the
        one policy consumers outside the demand path (the user surface's
        gated data/handle reads) consult."""
        return self._grounding_strict

    # -- demand (the policy matrix) ------------------------------------------------

    async def demand(self, grid_path: str, coords: str) -> dict[str, FxtrID]:
        """Produce the task cell's validated current results, or raise.

        The policy matrix from the module docstring. Also the engine's
        :data:`~fxtr_experiment.ops_engine.DemandHook` — child demands
        recurse through here (a demand cycle therefore blocks in-process
        until interrupted; accepted v0 behavior, see the scheduling notes).
        Self-vouching needs no threading: a child demand's request edge is
        durable before the demand hook fires (the engine records it at
        commitment), so the gate's evaluation sees the whole cascade's
        demand structure in the state db. User demands add no edge (they
        never self-vouch; the target is their durable justification).

        Args:
            grid_path: The task grid's key-path text.
            coords: The cell's flattened-coords text.

        Returns:
            The complete-and-fresh generation's per-output results — the
            value-pinned fact from one snapshot that also carried both
            gate verdicts. Consumers (the
            engine) serve and record exactly this map; there is no
            separate read to race against the check.

        Raises:
            CellFaultError: The cell is stale (clear it to re-run) or
                unsettled (the message names the support root); its run
                failed (after the errored retry); or a pre-run dependency
                was missing (no grid, no task fn).
            UngroundedSubjectError: The strict grounding gate refused the
                serve (the subject does not trace to current user intent).
        """
        key = (grid_path, coords)
        while (event := self._inflight.get(key)) is not None:
            await event.wait()  # "running → await it", then re-resolve
        # Register BEFORE the first await: the check-then-register window
        # must contain no suspension point, or two concurrent demands of one
        # cell could both pass the while-loop and double-run the generation.
        # Registering first is harmless on the serve path — the finally
        # clause releases either way.
        event = anyio.Event()
        self._inflight[key] = event
        try:
            report = await self._serve_gate(grid_path, coords)
            if report is not None and report.status == "complete":
                assert report.results is not None  # complete ⇒ snapshot results
                return dict(report.results)
            status = await self._run_once(grid_path, coords)
            if status != "complete":
                failures = await self._current_failures(grid_path, coords)
                raise CellFaultError(f"task cell {grid_path}{coords} is {status}: {failures}")
            # The on-arrival check: v0 lets a running-but-stale generation
            # finish, but a demand never returns a non-fresh (or, under
            # strict, ungrounded) result — and what it returns is the
            # checked snapshot's value.
            report = await self._serve_gate(grid_path, coords)
            if report is None or report.status != "complete" or report.results is None:
                raise CellFaultError(f"task cell {grid_path}{coords} was cleared while completing")
            return dict(report.results)
        finally:
            del self._inflight[key]
            event.set()

    async def _serve_gate(self, grid_path: str, coords: str) -> "CurrencyReport | None":
        """Both axes' strict gates over the cell's current generation, in
        one serve-or-fail evaluation (`WorkflowSession.serve_check`: one
        lock hold, verdicts and servable results from one snapshot).

        Currency: a non-fresh generation faults whatever its status (the
        strict v0 policy). Grounding (when the policy is strict): a
        *complete* generation about to be served must be grounded — the
        demander's own request edge is already durable, so a cascade
        vouches itself through the state db alone — evaluated at serve
        time, so a mid-run revocation upstream faults the demander at
        arrival, exactly like currency's re-check. Non-complete
        generations pass through ungated (nothing is being served; the
        run's own reads are gated as they happen).

        Returns:
            The snapshot report, or ``None`` if the cell has no current
            generation (a run is needed).

        Raises:
            CellFaultError: Stale or unsettled.
            UngroundedSubjectError: Strict policy, complete generation,
                not grounded.
        """
        currency, grounding = await self._session.serve_check(
            grid_path, coords, grounding=self._grounding_strict
        )
        if currency is not None and currency.currency != "fresh":
            raise CellFaultError(
                f"task cell {grid_path}{coords} is {currency.currency}: {currency.reason}"
            )
        if (
            self._grounding_strict
            and currency is not None
            and currency.status == "complete"
            and (grounding is None or grounding.verdict != "grounded")
        ):
            verdict = grounding.verdict if grounding is not None else "unjudgeable"
            reason = grounding.reason if grounding is not None else "no grounding verdict"
            raise UngroundedSubjectError(f"task cell {grid_path}{coords} is {verdict}: {reason}")
        return currency

    async def _current_failures(self, grid_path: str, coords: str) -> str:
        """Failure summary of the cell's current generation (diagnostics)."""
        row = await self._session.get_generation(grid_path, coords)
        if row is None:
            return "no generation"
        rows = await self._session.load_failures(row[0])
        return "; ".join(f"{cls}: {msg}" for _, cls, msg in rows) or "no failure recorded"

    # -- one run ---------------------------------------------------------------------

    async def _run_once(self, grid_path: str, coords: str) -> TerminalStatus:
        """Run (or resume) the cell's current generation once.

        Returns:
            The terminal status this run left the generation in
            (``complete`` / ``errored`` / ``interrupted``).

        Raises:
            CellFaultError: A pre-run dependency was missing (no grid at
                ``grid_path``, not a task grid, no task-fn cell, or the
                rule references a deleted/missing grid) — nothing was
                attempted, so no generation state is touched. Grants mint
                *before* the generation row is touched precisely so this
                holds: a mint failure must not strand a fresh row in
                ``running`` or erase a prior attempt's failure evidence.
                Mint failures carry the grid's grounding diagnosis when
                it is not grounded (the structural gate's immediate,
                richer message).
        """
        config = await self._task_config(grid_path)
        registry = SpecRegistry()
        try:
            grants = await mint_rule_grants(
                registry,
                config,
                grid_path,
                self._config_lookup,
                holder_coords=decode_coords(coords),
            )
        except (CellFaultError, ViewAxisError) as exc:
            # The structural mint gate's diagnosis upgrade: name the
            # grounding roots (e.g. which rule reference is missing or
            # mint-incompatible) alongside the raw mint failure. An
            # ungrounded grid means the *world* drifted under the stored
            # rule — a not-ready dependency, so the enriched error is
            # fault-class whatever minting raised (a structural
            # ViewAxisError against a grounded grid, by contrast, stays
            # what it was: mint_compatible is a grounded_grid conjunct,
            # so that case is not reachable from world drift).
            report = await self._session.grid_grounding(grid_path)
            if report is not None and report.verdict != "grounded":
                raise CellFaultError(
                    f"{exc} (grid {grid_path} is {report.verdict}: {report.reason})"
                ) from exc
            raise
        exec_id, generation, executed_fn = await self._get_or_mint_generation(
            grid_path, coords, config
        )
        engine: OpsEngine | None = None
        try:
            async with FxtrScope(
                name="wf3-run", isolated=True, include=[self._runner_scope]
            ) as run_scope:
                # The scope opens before the engine loads so the StepHost can
                # carry it: per-step scopes are isolated children of the run
                # scope, and steps' connect spawns resolve through it (the
                # run scope includes the runner scope = the registry's scope).

                async def read_gate(g: str, x: str) -> FxtrID | None:
                    # Data/handle reads: serve-or-fail from one snapshot.
                    # The reading cascade's request edges are already
                    # durable, so the gate's evaluation vouches writers
                    # from the state db alone.
                    return await self._session.gated_read_cell(g, x, strict=self._grounding_strict)

                engine = await OpsEngine.load(
                    self._session,
                    registry,
                    exec_id=exec_id,
                    generation=generation,
                    holder_coords=decode_coords(coords),
                    demand_hook=self.demand,
                    read_gate=read_gate,
                    step_host=StepHost(scope=run_scope, bindings=self._bindings),
                    # Every run restarts the cell's run-log buffer (the
                    # restart-per-run contract; the safety pass's frozen
                    # engine gets no sink and stays silent).
                    run_log=self.run_log.start_run(grid_path, coords, generation),
                )
                # The run prologue: the one recorded fn observation, through
                # the engine like every other op (hits on every replay, so
                # the safety pass needs no exemption for it).
                fn_key = await engine.read_handle_key(ViewSel(token=grants.task_fn.token))
                if fn_key != executed_fn:
                    # Safety check on the mint-time bookkeeping read: the fn
                    # cell changed between minting and the prologue (a
                    # concurrent user edit). The prologue observation is the
                    # truth; executed_fn would misreport what ran — don't
                    # run. (On a resume this cannot fire: the prologue hits
                    # the record, which is what executed_fn was set from.)
                    await self._session.record_failure(
                        exec_id,
                        FAILURE_FAULT,
                        "TaskFnChanged",
                        "task fn cell changed while the run was starting",
                    )
                    await self._session.set_status(exec_id, "interrupted")
                    return "interrupted"
                # Membership: spawn resolves the connector in the run scope,
                # which includes the runner scope (= the registry's scope);
                # the spawned ref is chain-bridged into the run scope.
                fn_ref = await self._bindings.spawn(fn_key, into_scope=run_scope)
                cap_ref = await run_scope.mount(RunCapabilityImpl(engine))
                proxy = await fn_ref.replace_service_type(WorkflowTaskFnProto).resolve(run_scope)
                results = await proxy.run_task(
                    cap_ref, dict(grants.args), grants.internals, config.outputs
                )
                # Request = observation: the run is not over until every
                # committed op has resolved (in-process this is usually
                # already true by structure; load-bearing for RPC).
                await engine.wait_obligations_drained()
                if not engine.run_failures:
                    try:
                        # Host-side output validation + frame identity
                        # minting (frame entities stage into the run scope,
                        # persisted by record_result below).
                        result_ids = _canonical_result_ids(dict(results), config.outputs, run_scope)
                    except TaskOutputError as exc:
                        # A task-output defect (bad names / types / frame
                        # payload): error-class, named as what it is —
                        # never mislabeled TaskRaisedError by the generic
                        # escape arm below.
                        await self._session.record_failure(
                            exec_id, FAILURE_ERROR, type(exc).__name__, str(exc)
                        )
                        await self._session.set_status(exec_id, "errored")
                        return "errored"
                    # Persist inside the run scope: the returned ids'
                    # closures ride the return arena (frame entities: the
                    # staging above) and die with it.
                    for name, value in result_ids.items():
                        await self._session.record_result(
                            exec_id, name, value, from_scope=run_scope
                        )
        except anyio.get_cancelled_exc_class():
            # Cut off between ops: no engine-recorded failure exists, so
            # record the fault-class evidence ourselves — under a narrow
            # shield, since cancellation is already pending — then let the
            # cancellation proceed (never block on it). (`engine` is None
            # only when the cut landed during the engine load itself.)
            with anyio.CancelScope(shield=True):
                if engine is None or not engine.run_failures:
                    await self._session.record_failure(
                        exec_id, FAILURE_FAULT, "RunCancelled", "run cancelled externally"
                    )
                await self._session.set_status(exec_id, "interrupted")
            raise
        except Exception as exc:
            if engine is None:
                # The engine load itself failed: a runner-side bug or storage
                # fault, not task logic (the fn was never invoked).
                await self._session.record_failure(
                    exec_id, FAILURE_ERROR, "InternalRunnerError", str(exc)
                )
                await self._session.set_status(exec_id, "errored")
                return "errored"
            if not engine.run_failures:
                # Escaped with no engine-recorded failure: the shared
                # escape rule (`_record_escape`). The one runner-side
                # typed raiser outside the engine's ops is the task-fn
                # spawn (its binding is missing/vanished) — fault-class;
                # anything else is by construction the task's own logic
                # (every op failure is recorded raise-side), and nothing
                # durable is classified from `exc` — its transit form is
                # display-only in the failure record.
                status = await self._record_escape(
                    exec_id, exc, error_kind="TaskRaisedError", error_message=str(exc)
                )
                await self._session.set_status(exec_id, status)
                return status
            status = self._status_from_failures(engine)
            await self._session.set_status(exec_id, status)
            return status
        assert engine is not None  # bound first inside the run scope
        if engine.run_failures:
            # The task returned normally around a poisoned run (caught or
            # dropped its failures) — the failure set wins; no results were
            # persisted above.
            status = self._status_from_failures(engine)
            await self._session.set_status(exec_id, status)
            return status
        status = await self._safety_pass(grid_path, coords, config, exec_id, generation)
        await self._session.set_status(exec_id, status)
        return status

    # -- the safety pass ---------------------------------------------------------------

    async def _safety_pass(
        self, grid_path: str, coords: str, config: TaskGridConfig, exec_id: int, generation: str
    ) -> TerminalStatus:
        """The always-on determinism check gating completion.

        Re-runs the outer level against a **frozen** engine (record misses
        raise instead of going live — pure compute by construction) and
        requires

        1. every effectful op is a hit (enforced by the frozen frontier),
        2. the replay's visited set equals the recorded set — observation
           subjects ∪ checkpoint subjects (nothing recorded is left
           unvisited — the run prologue replays too, so no subject is
           exempt; ``run_step`` adds checkpoint subjects to ``visited``,
           and its one-call rule closes the multiplicity blind spot a set
           comparison alone would have),
        3. the replay's would-create claim set equals the generation's
           stored ``grid_assertions`` (the frozen engine collects claims
           without inserting — the pass stays pure compute),
        4. the generation's stored ``request_edges`` match its recorded
           reads, cell for cell — every request answered, every read
           requested. Requests are recorded when the run asks,
           observations when the answer arrives, so an interrupted run
           can have asked-but-unanswered requests on file; a *completed*
           one may not (a resume that skips a previously-asked request
           has diverged). A pure DB-side comparison, sound given (2);
           the invariant note is in ``notes/execution_semantics.md``.
        5. the replayed outputs equal the recorded results.

        Steps replay as checkpoint hits — the pass never invokes a step
        fn, so the replay engine keeps ``step_host=None``: a checkpoint
        miss cannot legitimately happen (the frozen frontier fires first),
        and if a frontier hole ever let one through, the hostless miss
        raises :class:`InternalRunnerError` — the same bug-not-user-code
        layering as the refusing demand hook below. Invocation equality on
        replayed hits is ``run_step``'s own hit-mismatch check
        (``NondeterminismError``, recorded by the op wrapper). The replay
        re-mounts task-side step adapters and re-stages args payload
        entities per call — both harmless (never invoked; unpersisted
        staging).

        The **refusing demand hook** is not load-bearing on top of (1) —
        the frontier guard fires before any miss can reach the hook — but
        the (mandatory) hook slot must hold something, and the refuser is
        the least-authority choice: the pass's engine has no authority to
        demand, so a hole in the frontier guard (e.g. a future op's miss
        path forgetting its ``_guard_frontier`` call) degrades to a loud
        :class:`InternalRunnerError` — recorded and surfaced as a *system
        bug*, deliberately distinct from the ``NondeterminismError`` path
        that flags misbehaving user code — rather than silently executing
        live upstream work.

        Returns:
            The terminal status the pass decided (the caller sets it):
            ``complete`` if the generation is deterministic-given-the-
            record; ``errored`` on a violation (a ``NondeterminismError``
            failure, recorded raise-side if an engine op raised it,
            executor-side for the set/output comparisons); ``interrupted``
            on a fault-class escape — the pass re-invokes the task fn, so
            its setup shares a run's not-ready surface, and those escapes
            classify by the shared rule (:meth:`_record_escape`): they
            mean "couldn't finish", never "finished differently".
        """

        async def refuse(path: str, cell: str) -> dict[str, FxtrID]:
            raise InternalRunnerError(
                f"safety-pass frontier hole: the demand hook was reached for"
                f" {path}{cell} — some op's miss path is missing its _guard_frontier"
                " guard (the frozen engine should have raised first)"
            )

        replay: "OpsEngine | None" = None
        try:
            # Setup inside the try: the pass gates completion, so *any*
            # failure here (e.g. a rule-referenced grid deleted between the
            # run and the pass) must record evidence and land errored —
            # never propagate with the generation still marked running.
            registry = SpecRegistry()
            grants = await mint_rule_grants(
                registry,
                config,
                grid_path,
                self._config_lookup,
                holder_coords=decode_coords(coords),
            )
            replay = await OpsEngine.load(
                self._session,
                registry,
                exec_id=exec_id,
                generation=generation,
                holder_coords=decode_coords(coords),
                demand_hook=refuse,
                frozen=True,
            )
            recorded = {(r[0], r[1], r[2]) for r in await self._session.load_record(generation)}
            # Checkpoint subjects join the comparison set (docstring point 2);
            # they key as (grid_path, coords, "") — no output component.
            recorded |= {(c[0], c[1], "") for c in await self._session.load_checkpoints(exec_id)}
            async with FxtrScope(
                name="wf3-safety-pass", isolated=True, include=[self._runner_scope]
            ) as scope:
                fn_key = await replay.read_handle_key(ViewSel(token=grants.task_fn.token))
                fn_ref = await self._bindings.spawn(fn_key, into_scope=scope)
                cap_ref = await scope.mount(RunCapabilityImpl(replay))
                proxy = await fn_ref.replace_service_type(WorkflowTaskFnProto).resolve(scope)
                raw_outputs = dict(
                    await proxy.run_task(
                        cap_ref, dict(grants.args), grants.internals, config.outputs
                    )
                )
                await replay.wait_obligations_drained()
                # Canonicalize before comparing: a replayed task fn
                # legitimately reassembles frame rows in a different order
                # (multiset identity exists precisely for this), so frame
                # outputs compare as canonical ids, never raw IPC bytes.
                # Pure compute: the scratch scope stages the entities to
                # learn their ids and nothing is persisted; a canonicalize
                # failure lands in the except arm like any replay failure.
                outputs = _canonical_result_ids(raw_outputs, config.outputs, scope)
        except Exception as exc:
            if replay is not None and replay.run_failures:
                # An engine-op violation was recorded raise-side (frozen
                # frontier / refused demand / hit mismatch): the failure
                # set is ground truth, derived exactly like a run's.
                return self._status_from_failures(replay)
            # Escaped with nothing recorded: the shared escape rule. The
            # pass re-invokes the task fn, so its setup (grant minting,
            # the task-fn spawn/connect) can find the world not ready —
            # e.g. the binding or a rule-referenced grid vanished between
            # runs (then `replay` may still be None) — and those faults
            # land interrupted; anything else is the pass's own error
            # class, recorded so the errored status has its evidence.
            return await self._record_escape(
                exec_id,
                exc,
                error_kind="NondeterminismError",
                error_message=f"safety pass replay failed: {exc}",
            )
        assert replay is not None  # bound first inside the try
        if replay.run_failures:
            # Violation recorded raise-side (the task caught or dropped
            # it); same ground-truth derivation as a run's failure set.
            return self._status_from_failures(replay)
        if replay.visited != recorded:
            missing = sorted(recorded - replay.visited)
            extra = sorted(replay.visited - recorded)
            await self._record_safety_failure(
                exec_id,
                f"safety pass op-set mismatch: recorded-but-not-replayed {missing};"
                f" replayed-but-unrecorded {extra}",
            )
            return "errored"
        # Would-create claims join the comparison: the frozen engine only
        # collects them (pure compute), so the replay's set must equal the
        # generation's stored monotone-OR — a divergence means the opens'
        # authority pattern was nondeterministic.
        recorded_assertions = set(await self._session.load_assertions(exec_id))
        if replay.assertions != recorded_assertions:
            missing_a = sorted(g for g, _ in recorded_assertions - replay.assertions)
            extra_a = sorted(g for g, _ in replay.assertions - recorded_assertions)
            await self._record_safety_failure(
                exec_id,
                f"safety pass assertion-set mismatch: recorded-but-not-replayed"
                f" {missing_a}; replayed-but-unrecorded {extra_a}",
            )
            return "errored"
        # Request edges join the comparison (docstring point 4): every
        # request answered, every read requested. A dangling edge is an
        # aborted request the resumed run never re-asked (nondeterminism);
        # a missing edge is a miss path that skipped its insert (a system
        # bug — the same comparison catches it).
        requested = set(await self._session.load_request_edges(exec_id))
        recorded_reads = {
            (r[0], r[1], REQ_KIND_TASK_OUTPUT if r[2] else REQ_KIND_DATA)
            for r in await self._session.load_record(generation)
            if r[4] == OBS_KIND_CELL_VALUE
        }
        if requested != recorded_reads:
            dangling = sorted(f"{g}{c}" for g, c, _ in requested - recorded_reads)
            unrequested = sorted(f"{g}{c}" for g, c, _ in recorded_reads - requested)
            await self._record_safety_failure(
                exec_id,
                f"safety pass request-edge mismatch: requested-but-not-observed"
                f" {dangling}; observed-but-not-requested {unrequested}",
            )
            return "errored"
        if outputs != await self._session.get_results(exec_id):
            await self._record_safety_failure(exec_id, "safety pass produced different outputs")
            return "errored"
        return "complete"

    async def _record_safety_failure(self, exec_id: int, message: str) -> None:
        """Record one executor-side safety-pass comparison violation.

        Reached only with an empty raise-side failure set — the pass
        derives from the failure set and returns before the comparisons
        otherwise — so this records unconditionally."""
        await self._session.record_failure(exec_id, FAILURE_ERROR, "NondeterminismError", message)

    async def _record_escape(
        self, exec_id: int, exc: Exception, *, error_kind: str, error_message: str
    ) -> TerminalStatus:
        """Classify + record an exception that escaped a run or pass body
        with no engine-recorded failure — the one escape-classification
        rule, shared by :meth:`_run_once` and :meth:`_safety_pass`.

        A runner-side typed ``CellFaultError`` means the world wasn't
        ready (a not-ready binding, dependency, or grid): fault-class
        failure, ``interrupted``, retryable. Anything else is error-class
        under the caller's label (``TaskRaisedError`` for a run, the
        pass's ``NondeterminismError``). Classification comes from the
        typed exception raised in this process, never from a transit
        form: task-side raises arrive as ``IRException`` and take the
        error arm (the error-transit doctrine, `errors.py`).

        Returns:
            The terminal status the caller should set for the generation.
        """
        if isinstance(exc, CellFaultError):
            await self._session.record_failure(exec_id, FAILURE_FAULT, type(exc).__name__, str(exc))
            return "interrupted"
        await self._session.record_failure(exec_id, FAILURE_ERROR, error_kind, error_message)
        return "errored"

    @staticmethod
    def _status_from_failures(engine: OpsEngine) -> TerminalStatus:
        """``errored`` iff any error-class failure; else ``interrupted``.

        Only consulted with a non-empty failure set; classification comes
        from the engine's raise-side records, never from anything that
        crossed the wire.
        """
        assert engine.run_failures
        kinds = {kind for kind, _, _ in engine.run_failures}
        return "errored" if FAILURE_ERROR in kinds else "interrupted"

    async def _get_or_mint_generation(
        self, grid_path: str, coords: str, config: TaskGridConfig
    ) -> tuple[int, str, FxtrID]:
        """The cell's current generation, minting a fresh one if absent.

        Minting needs the fn cell's current key to populate the
        ``current_fn``/``executed_fn`` columns — read via the session as
        pure bookkeeping (the *observation* is the run prologue's engine
        read, which is checked against the returned ``executed_fn``). A
        later run clears the previous attempt's failure records and flips
        the status back to ``running``.

        Returns:
            ``(exec_id, generation, executed_fn)``.

        Raises:
            CellFaultError: No task-fn cell (a not-ready dependency;
                nothing is minted).
        """
        row = await self._session.get_generation(grid_path, coords)
        if row is None:
            fn_path, fn_coords = resolve_templates(
                config.rule.task_fn.path,
                config.rule.task_fn.target_axes,
                decode_coords(coords),
            )
            fn_key = await self._session.read_cell(fn_path, fn_coords)
            if fn_key is None:
                raise CellFaultError(f"no task fn at {fn_path}{fn_coords}")
            generation = new_generation_id()
            exec_id = await self._session.insert_generation(
                generation, grid_path, coords, fn_key, fn_key
            )
            return exec_id, generation, fn_key
        exec_id, generation, executed_fn = row[0], row[1], row[3]
        await self._session.clear_failures(exec_id)
        await self._session.set_status(exec_id, "running")
        return exec_id, generation, executed_fn

    # -- config plumbing ---------------------------------------------------------------

    async def _config_lookup(self, key_path: str) -> tuple[str, GridConfig]:
        """`spec_registry.ConfigLookup` over the session's live grids.

        Raises:
            CellFaultError: No grid at ``key_path`` (a rule references a
                missing grid — a not-ready dependency).
        """
        row = await self._session.get_grid(key_path)
        if row is None:
            raise CellFaultError(f"no grid at {key_path}")
        entity = await self._session.load_config(row[1])
        return row[0], entity.config

    async def _task_config(self, grid_path: str) -> TaskGridConfig:
        """The task grid's canonical config, kind-checked.

        Raises:
            CellFaultError: No grid, or not a task grid.
        """
        kind, config = await self._config_lookup(grid_path)
        if kind != "task" or not isinstance(config, TaskGridConfig):
            raise CellFaultError(f"{grid_path} is a {kind} grid, not a task grid")
        return config
