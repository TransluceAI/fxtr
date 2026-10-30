"""The two-file durable session: workflow state db + fxtr entity store.

:class:`WorkflowSession` opens both storage files side by side and is the
typed layer the runner talks to:

- The **state db** (`storage.py` schema) via one dedicated sqlite
  connection, guarded by an ``anyio.Lock`` and driven through worker
  threads — one transaction per logical op. (v0 is single-writer; the
  fxtr2 connection-pool utils can replace this if concurrency ever wants
  it.)
- The **entity store** (an fxtr2 ``SQLiteFxtrNamespace``) holding every
  entity the state db references. The **liveness invariant** is enforced
  here: :meth:`WorkflowSession.persist` pins an entity's closure (pin key
  = the id string, idempotent) *before* any method records its id — the
  combined helpers (:meth:`write_cell`, :meth:`put_checkpoint`, ...) do
  both in the right order, so the state db can never dangle. A crash
  between pin and record leaves only an unreferenced (still pinned)
  entity, harmless until version-log GC exists.

Startup performs the crashed-run flip: any ``running`` generation found
when the session opens becomes ``interrupted`` (see
``notes/execution_semantics.md``).

**The session owns currency maintenance.** Currency verdicts are cached
as *stamps* on ``task_execs`` rows (`storage.py`), and the invariant that
keeps a live stamp trustworthy — every settled, currency-relevant change
clears the stamps it could invalidate, in the same transaction — is
maintained *here*: each mutation body below declares what it marks
("Marks:" in its docstring; the trigger table is in
``notes/execution_semantics.md``), and :meth:`WorkflowSession.
cell_currency` is the one verification/serve entry point (its
implementation lives in `currency.py`). There is deliberately no public
"run a body under my lock" seam — the session maintains its own derived
state, and composite snapshot reads exist only as typed methods.

Identity arguments are the canonical text/blob forms from `encoding.py`
(key-path text, flattened-coords text, FxtrID); this layer stays dumb
about views/specs — it is storage, not semantics.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import sqlite3
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Mapping, Sequence, TypeVar, cast

import anyio
import anyio.to_thread
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import Entity, EntityRef, FxtrScope, get_active_scope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

from fxtr_experiment.change_hints import Hint, collect_hints, note_change
from fxtr_experiment.currency import CurrencyReport, evaluate_cell
from fxtr_experiment_task.errors import (
    GridConfigMismatchError,
    GridNotSourcedError,
    HandleAlreadyBoundError,
    SandboxDependencyError,
    UngroundedSubjectError,
    UnknownBindingError,
)
from fxtr_experiment.export import ExportSnapshot, export_snapshot_body
from fxtr_experiment.importer import ImportBundle, ImportReport, apply_import_body
from fxtr_experiment.grounding import (
    GroundingReport,
    Seed,
    clear_gen_grounding_stamp,
    delete_wanted_stamps_under,
    evaluate_component,
    load_component,
    mark_grounding,
    would_start_fan_seeds,
)
from fxtr_experiment.encoding import (
    GenerationRecordEntity,
    GridConfigEntity,
    SliceTemplateEntity,
    StepPayloadEntity,
    canonical_generation_record,
    decode_key_path,
    frozen_cbor_to_service_data,
    generation_record_ids,
    parent_key_path,
    validate_grid_key,
)
from fxtr_experiment.introspect import (
    AxisCorrespondenceRow,
    GenerationDetail,
    GraphEdgeRow,
    GridSummaryRow,
    InputGrantRow,
    ObserverRow,
    build_graph_structure,
    collect_graph_edges,
    collect_grid_bases,
    collect_grid_configs,
    generation_detail,
    observer_cells,
    task_cell_locations,
)
from fxtr_experiment.lint import (
    LintState,
    LintTargetRow,
    LintUngroundedRow,
    collect_facts as lint_collect_facts,
    collect_targets as lint_collect_targets,
    dangling_sourcing_rows as lint_dangling_sourcing,
)

if TYPE_CHECKING:
    from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr_experiment_task.model import (
    AssertionFact,
    CheckpointFact,
    FailureFact,
    GenerationRecord,
    GridConfig,
    ObservationFact,
    RequestFact,
    SandboxDefn,
    TerminalStatus,
)
from fxtr_experiment.storage import (
    FAILURE_KIND_CODES,
    FAILURE_KIND_NAMES,
    OBS_KIND_NAMES,
    OBS_KIND_WROTE_CELL,
    REQ_KIND_NAMES,
    REQ_KIND_TASK_OUTPUT,
    SCHEMA_VERSION,
    create_workflow_state_schema,
    id_blob,
    id_of,
)

_R = TypeVar("_R")

ENTITY_NAMESPACE_NAME = "workflow"

DEFAULT_FRAME_CACHE_BYTES = 256 * 2**20
"""Default byte budget of the in-memory frame projection (256 MiB)."""


class StateDbError(RuntimeError):
    """Integrity/usage errors raised by the state-db layer."""


@dataclass(frozen=True)
class SandboxDepRow:
    """One pinned dependency of a sandboxed binding, as stored.

    Attributes:
        name: The dependency name the guest receives.
        grid_path / coords: The pinned handle cell's absolute location
            (path text; no FK — the cell may since have been deleted).
        pinned_key_id: The handle key read from the cell at bind time —
            the compatibility pin re-checked at connect (chunk 3).
    """

    name: str
    grid_path: str
    coords: str
    pinned_key_id: FxtrID


@dataclass(frozen=True)
class SandboxBindingRow:
    """One durable sandboxed-binding record, as stored (deps included).

    Attributes:
        key_id: The bound handle key id (the binding identity).
        defn: The nix flake reference triple.
        config_json: The binding's config as canonical JSON text, or
            ``None``.
        deps: The pinned dependencies, ``name``-sorted (the canonical
            order — deps are an unordered map).
    """

    key_id: FxtrID
    defn: SandboxDefn
    config_json: str | None
    deps: tuple[SandboxDepRow, ...]


class FrameProjection:
    """The in-memory frame projection: frame entity id → arrow IPC bytes.

    Pure cache (``notes/dataframes.md``): derived, rebuildable from the
    canonical entity, never exported — and content addressing makes
    invalidation a non-issue, so the only policy is a memory bound. That
    bound is a **bytes budget** (entry cost = ``len(bytes)``; frame sizes
    span orders of magnitude, so an entry-count cap would be dishonest),
    evicting least-recently-used; an entry larger than the whole budget is
    served uncached. This object is the seam a persistent projection
    (lmdb / sqlite blob sidecar — a deferred followup) drops in behind.

    Not thread-safe on its own; the session calls it under its serving
    paths (single-runner v0).
    """

    def __init__(self, budget: int = DEFAULT_FRAME_CACHE_BYTES) -> None:
        """An empty projection with the given byte budget."""
        self._budget = budget
        self._total = 0
        self._entries: "OrderedDict[FxtrID, bytes]" = OrderedDict()

    def get(self, frame_id: FxtrID) -> bytes | None:
        """The cached bytes for a frame id (refreshes recency), or None."""
        data = self._entries.get(frame_id)
        if data is not None:
            self._entries.move_to_end(frame_id)
        return data

    def put(self, frame_id: FxtrID, data: bytes) -> None:
        """Insert one rebuilt projection entry, evicting LRU past budget.

        Idempotent per id (content-addressed: one id, one canonical
        payload — a re-put only refreshes recency); an oversize entry is
        dropped rather than evicting the whole cache for one tenant.
        """
        if frame_id in self._entries:
            self._entries.move_to_end(frame_id)
            return
        if len(data) > self._budget:
            return
        self._entries[frame_id] = data
        self._total += len(data)
        while self._total > self._budget:
            _, evicted = self._entries.popitem(last=False)
            self._total -= len(evicted)


class WorkflowSession:
    """One open workflow: state db + entity store + their scope wiring.

    Construct via :meth:`open`. The entity namespace's scope is a parent of
    whatever scope the caller runs under it (standard fxtr2 inheritance),
    so ids recorded in the state db stay loadable below.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        namespace: SQLiteFxtrNamespace,
        *,
        frame_cache_bytes: int = DEFAULT_FRAME_CACHE_BYTES,
    ) -> None:
        """Bare constructor — use :meth:`open` (schema init + startup flip)."""
        self._conn = conn
        self._namespace = namespace
        self._lock = anyio.Lock()
        self._event_listener: "Callable[[frozenset[Hint]], None] | None" = None
        self._frames = FrameProjection(frame_cache_bytes)

    def set_event_listener(self, listener: "Callable[[frozenset[Hint]], None] | None") -> None:
        """Install (or clear, with ``None``) the change-hint listener.

        One listener (the capability's event hub in the intended
        assembly); installing over a live one raises — the seam is not a
        broadcast layer, fan-out belongs to the subscriber hub behind it.
        The listener is called after a mutating op's transaction has
        committed, before the session lock releases (so deliveries can
        never observe cross-op reordering), with the op's deduped hint
        set — and it must not block: hand the set to an async worker.
        Hints for rolled-back transactions are never delivered
        (`change_hints.collect_hints`).
        """
        if listener is not None and self._event_listener is not None:
            raise StateDbError("an event listener is already installed")
        self._event_listener = listener

    def _flush_hints(self, hints: "set[Hint]") -> None:
        """Deliver one op's hint set to the listener (post-commit)."""
        if hints and self._event_listener is not None:
            self._event_listener(frozenset(hints))

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        state_db_path: str,
        entity_db_path: str,
        *,
        frame_cache_bytes: int = DEFAULT_FRAME_CACHE_BYTES,
    ) -> AsyncIterator["WorkflowSession"]:
        """Open (or create) both files and yield the session.

        Args:
            state_db_path: Path of the workflow state db (created if absent).
            entity_db_path: Path of the fxtr entity store.
            frame_cache_bytes: Byte budget of the in-memory frame
                projection (:class:`FrameProjection`).

        Raises:
            StateDbError: On a schema-version mismatch.
        """
        async with SQLiteFxtrNamespace(entity_db_path, ENTITY_NAMESPACE_NAME) as ns:
            conn = sqlite3.connect(state_db_path, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA foreign_keys = ON")
                _init_schema(conn)
                session = cls(conn, ns, frame_cache_bytes=frame_cache_bytes)
                # The startup crashed-run flip, gapless per row: assemble
                # the record (RunnerCrash failure synthesized in memory),
                # persist it, then insert that failure row + flip status +
                # point record_id in ONE transaction — so terminal-with-
                # NULL-record is unrepresentable and a crash mid-open
                # leaves the row running with nothing written (a clean,
                # idempotent retry at the next open).
                for exec_id in await session._run(_running_exec_ids):
                    async with session._lock:
                        await session._record_generation_locked(
                            exec_id,
                            status_override="interrupted",
                            crash_failure=FailureFact(
                                kind="fault",
                                error_class="RunnerCrash",
                                message="runner crashed or was shut down mid-run",
                                ts=_now_iso(),
                            ),
                        )
                yield session
            finally:
                conn.close()

    @property
    def entity_scope(self) -> FxtrScope:
        """The entity namespace's scope (include/inherit to load recorded ids)."""
        return self._namespace.scope

    async def _run(self, fn: Callable[..., _R], *args: Any) -> _R:
        """Run a sync db body in a worker thread, serialized by the lock.

        A change-hint collector is installed around the body (anyio
        propagates the context into the worker thread, so the body's
        `change_hints.note_change` calls land in it) and flushed to the
        event listener only on clean return — the body's transaction has
        committed by then, and an exception (a rolled-back transaction)
        skips the flush entirely. Read-only bodies note nothing and
        flush nothing.

        Args:
            fn: A sync function taking the connection first (one of the
                module-level ``_``-bodies below; each owns its transaction).
            *args: Forwarded positionally after the connection.

        Returns:
            Whatever ``fn`` returns.
        """
        async with self._lock:
            with collect_hints() as hints:
                result = await anyio.to_thread.run_sync(fn, self._conn, *args)
            self._flush_hints(hints)
            return result

    # -- currency (the session-owned derived state) ---------------------------

    async def cell_currency(self, grid_path: str, coords: str) -> "CurrencyReport | None":
        """Evaluate one task cell's currency: serve the stamp or derive + stamp.

        One atomic body (worker thread, serialized by the session lock
        like every op), so the whole walk *and* the values the report
        carries are facts about a single snapshot — which is what makes
        "this (generation, results) pair is fresh" a coherent claim. A
        settled generation with a live stamp serves in O(1) (the marking
        invariant — see the module docstring — keeps live stamps
        current); anything else derives, and derived verdicts of settled
        generations are stamped back in the same transaction. The stamps
        *are* the cross-query memo: verifying one cell stamps its whole
        settled support cone, so subsequent queries hit stamps instead of
        re-walking. Implementation: `currency.evaluate_cell`.

        Args:
            grid_path: The task grid's key-path text.
            coords: The cell's flattened-coords text.

        Returns:
            The snapshot report for the cell's current generation, or
            ``None`` if the cell has no current generation.
        """
        return await self._run(evaluate_cell, grid_path, coords)

    # -- grounding (the second session-owned derived axis) ---------------------

    async def grid_grounding(self, grid_path: str) -> "GroundingReport | None":
        """One grid's grounding verdict: serve the stamp or derive + stamp.

        The intent-axis sibling of :meth:`cell_currency`; semantics and
        the correctness argument are `grounding.py`'s module docstring.

        Returns:
            The report, or ``None`` if no grid exists at the path.
        """
        return await self._grounding(("grid", grid_path))

    async def cell_grounding(self, grid_path: str, coords: str) -> "GroundingReport | None":
        """One cell's grounding verdict (task cells: the current
        generation's ``grounded_task_cell`` plus the cell's ``wanted``;
        data/handle cells: ``grounded_data_cell``).

        Returns:
            The report, or ``None`` when there is nothing to judge (no
            such grid, an empty cell, or a task cell with no generation —
            its ``wanted`` verdict still stamps as a side effect).
        """
        return await self._grounding(("cell", grid_path, coords))

    async def _grounding(self, root: tuple[str, ...]) -> "GroundingReport | None":
        async with self._lock:
            return await self._grounding_locked(root)

    async def _grounding_locked(self, root: tuple[str, ...]) -> "GroundingReport | None":
        """The two-phase evaluation; the caller holds the session lock.

        Load the justification component (phase 1, one transaction), load
        the component's config *entities* (immutable and content-addressed,
        so a mid-lock load cannot tear — `grounding.py` (d)), then run the
        LFP and stamp back (phase 2, one transaction). The caller's lock
        hold is what makes the phases one atomic session op (v0
        single-writer: nothing else touches the state db between them)."""
        comp = await anyio.to_thread.run_sync(load_component, self._conn, root)
        configs = {}
        for config_id in comp.config_ids:
            # Membership: config ids are pinned by create_grid's
            # persist (the liveness invariant), so the namespace's own
            # scope can retrieve and load them — same rationale as
            # load_config, which this reuses (it takes no state lock).
            configs[config_id] = (await self.load_config(config_id)).config
        return await anyio.to_thread.run_sync(evaluate_component, self._conn, comp, configs)

    async def cell_snapshot(
        self, grid_path: str, coords: str
    ) -> "tuple[CurrencyReport | None, GroundingReport | None, FxtrID | None]":
        """The one-snapshot inspection triple: currency (task cells),
        grounding (both axes' verdicts), and the raw cell value
        (data/handle cells), all under a single hold of the session lock —
        the introspection doctrine's "content and verdict are one
        coherent fact", extended to two axes. Non-faulting; the gate
        chunk's serve-or-fail op reuses this composition with a policy
        branch.
        """
        async with self._lock:
            currency = await anyio.to_thread.run_sync(evaluate_cell, self._conn, grid_path, coords)
            grounding = await self._grounding_locked(("cell", grid_path, coords))
            value = await anyio.to_thread.run_sync(_read_cell, self._conn, grid_path, coords)
            return currency, grounding, value

    async def lint(self) -> LintState:
        """The whole-workflow structural inventory at one snapshot.

        The clean-state check of the intent axis (the linter section of
        ``notes/grounding.md``): dangling sourcing records and targets,
        attached targets whose materialized fact is not grounded, and
        every non-grounded fact — all under a single hold of the session
        lock, so the report is one coherent fact. Grounding-only by
        design: findings are construction problems, never rerun needs
        (currency is not consulted). Records nothing (a privileged
        presence-class query); bulk derivation piggybacks on the stamps,
        so the first sweep pays the walk and repeats serve stamps.
        Enumeration bodies live in `lint.py`; per-fact verdicts come from
        :meth:`_grounding_locked`.

        Returns:
            The findings-only snapshot (ids raw — in-process callers need
            no coverage; the wire layer re-attaches).
        """
        async with self._lock:
            grids, dcells, tcells = await anyio.to_thread.run_sync(lint_collect_facts, self._conn)
            ungrounded: list[LintUngroundedRow] = []
            for path in grids:
                report = await self._grounding_locked(("grid", path))
                # Enumerated facts exist at this snapshot (single lock
                # hold), so the reports are never None in these loops.
                assert report is not None
                if report.verdict != "grounded":
                    ungrounded.append(
                        LintUngroundedRow("grid", path, "", report.verdict, report.reason)
                    )
            for grid_path, coords in dcells:
                report = await self._grounding_locked(("cell", grid_path, coords))
                assert report is not None
                if report.verdict != "grounded":
                    ungrounded.append(
                        LintUngroundedRow(
                            "data_cell", grid_path, coords, report.verdict, report.reason
                        )
                    )
            for grid_path, coords in tcells:
                report = await self._grounding_locked(("cell", grid_path, coords))
                assert report is not None
                if report.verdict != "grounded":
                    ungrounded.append(
                        LintUngroundedRow(
                            "task_cell", grid_path, coords, report.verdict, report.reason
                        )
                    )
            target_rows: list[LintTargetRow] = []
            for scan in await anyio.to_thread.run_sync(lint_collect_targets, self._conn):
                verdict, reason = None, None
                if scan.status == "attached":
                    if not scan.materialized:
                        continue  # pending intent, not a finding
                    report = await self._grounding_locked(("cell", scan.grid_path, scan.coords))
                    assert report is not None  # materialized ⇒ gradable
                    if report.verdict == "grounded":
                        continue
                    verdict, reason = report.verdict, report.reason
                target_rows.append(
                    LintTargetRow(
                        slice_id=scan.slice_id,
                        expected_config=scan.expected_config,
                        grid_path=scan.grid_path,
                        coords=scan.coords,
                        status=scan.status,
                        verdict=verdict,
                        reason=reason,
                    )
                )
            sourcing = await anyio.to_thread.run_sync(lint_dangling_sourcing, self._conn)
            return LintState(
                dangling_sourcing=sourcing,
                targets=tuple(target_rows),
                ungrounded=tuple(ungrounded),
            )

    async def summarize(self) -> tuple[GridSummaryRow, ...]:
        """Every grid with rollup counts and verdicts, one session snapshot
        — the dashboard tree's one-call backing (``notes/monitoring_ui.md``).

        One lock hold, like :meth:`lint`: the SQL-side counts load first,
        then the currency **force-evaluation pass** derives every task
        cell's verdict (`currency.evaluate_cell` — the stamps are the
        memo, so a repeat summary serves O(1) per settled cell and a cold
        namespace pays the walk once), then per-grid grounding verdicts
        come from the shared evaluation. Presence-class: records nothing,
        nothing downstream can depend on it; a privileged whole-namespace
        enumeration like lint, deliberately outside the open-map rule.

        Returns:
            One row per existing grid, in key-path order (ids raw —
            in-process callers need no coverage; the wire layer
            re-attaches).
        """
        async with self._lock:
            bases = await anyio.to_thread.run_sync(collect_grid_bases, self._conn)
            stale: dict[str, int] = {}
            unsettled: dict[str, int] = {}
            for grid_path, coords in await anyio.to_thread.run_sync(
                task_cell_locations, self._conn
            ):
                report = await anyio.to_thread.run_sync(
                    evaluate_cell, self._conn, grid_path, coords
                )
                # Enumerated cells exist at this snapshot (single lock
                # hold), so the report is never None here.
                assert report is not None
                if report.currency == "stale":
                    stale[grid_path] = stale.get(grid_path, 0) + 1
                elif report.currency == "unsettled":
                    unsettled[grid_path] = unsettled.get(grid_path, 0) + 1
            rows = []
            for base in bases:
                grounding = await self._grounding_locked(("grid", base.path))
                assert grounding is not None  # just enumerated ⇒ gradable
                rows.append(
                    GridSummaryRow(
                        path=base.path,
                        kind=base.kind,
                        config_id=base.config_id,
                        grounding_verdict=grounding.verdict,
                        grounding_reason=grounding.reason,
                        sourcing=base.sourcing,
                        populated=base.populated,
                        n_pending=base.n_pending,
                        n_running=base.n_running,
                        n_complete=base.n_complete,
                        n_errored=base.n_errored,
                        n_interrupted=base.n_interrupted,
                        n_stale=stale.get(base.path, 0),
                        n_unsettled=unsettled.get(base.path, 0),
                        targets_attached=base.targets_attached,
                        targets_dangling=base.targets_dangling,
                    )
                )
            return tuple(rows)

    async def graph_edges(self) -> tuple[GraphEdgeRow, ...]:
        """The grid-level relationship edges (the plate view's overlay —
        ``notes/plate_view.md``). Presence-class: records nothing, one lock
        hold, ids-free. Recomputes on ``cells`` hints (edges turn over with
        generations)."""
        async with self._lock:
            return await anyio.to_thread.run_sync(collect_graph_edges, self._conn)

    async def graph_structure(
        self,
    ) -> tuple[tuple[InputGrantRow, ...], tuple[AxisCorrespondenceRow, ...]]:
        """The rule-derived overlay half: input grants + ``Param`` axis
        correspondences (``notes/plate_view.md``).

        The grid → config-id map is snapshotted under the lock; the configs
        are then loaded (``load_config`` takes no state lock and configs are
        immutable content-addressed entities, so the post-snapshot load is a
        coherent read) and the pure builder derives the structure. Depends
        only on the grid set — a generation turnover does not change it — so
        the watch recomputes this half on ``grids`` hints only.
        """
        async with self._lock:
            config_rows = await anyio.to_thread.run_sync(collect_grid_configs, self._conn)
        configs: dict[str, GridConfig] = {
            row.path: (await self.load_config(row.config_id)).config for row in config_rows
        }
        return build_graph_structure(configs)

    async def generation_report(self, grid_path: str, coords: str) -> "GenerationDetail | None":
        """One task cell's current generation, itemized: failures,
        observations with per-edge current-match flags, unanswered
        requests, checkpoint entries — one transaction, one snapshot
        (`introspect.generation_detail`). Presence-class: records
        nothing.

        Returns:
            The detail (ids raw — the wire layer re-attaches), or
            ``None`` when the cell has no current generation.
        """
        return await self._run(generation_detail, grid_path, coords)

    async def list_observers(
        self, grid_path: str, coords: str, output: str | None = None
    ) -> tuple[ObserverRow, ...]:
        """The observer cells of one subject, with status and currency.

        Reverse edges over the identity index (location → versions →
        edges → observer generations; `introspect.observer_cells`),
        composed with per-observer currency evaluation under one lock
        hold (the stamps memoize across observers, exactly as in
        :meth:`summarize`). ``output`` narrows to one task-output
        subject. Presence-class: records nothing.

        Returns:
            One row per observer cell, in (path, coords) order.
        """
        async with self._lock:
            cells = await anyio.to_thread.run_sync(
                observer_cells, self._conn, grid_path, coords, output
            )
            rows = []
            for obs_path, obs_coords in cells:
                report = await anyio.to_thread.run_sync(
                    evaluate_cell, self._conn, obs_path, obs_coords
                )
                # Observer rows come from task_execs (just enumerated,
                # same lock hold), so the report is never None.
                assert report is not None
                rows.append(
                    ObserverRow(
                        grid_path=obs_path,
                        coords=obs_coords,
                        status=report.status,
                        currency=report.currency,
                    )
                )
            return tuple(rows)

    async def has_attached_target(self, grid_path: str, coords: str) -> bool:
        """Whether an *attached* target names the cell (expected config ==
        the grid's current config) — the user-demand precondition."""
        return await self._run(_has_attached_target, grid_path, coords)

    async def serve_check(
        self,
        grid_path: str,
        coords: str,
        *,
        grounding: bool,
    ) -> "tuple[CurrencyReport | None, GroundingReport | None]":
        """The task-output serve gate's evaluation: both axes under one
        lock hold (no check-then-use window — the currency report carries
        the servable results from the same snapshot the verdicts derive
        on). A running cascade's demand is already durable (request edges
        are written at commitment), so the evaluation needs no extras.
        The caller (the executor's gate) applies the policy and raises;
        this op only evaluates.
        """
        async with self._lock:
            currency = await anyio.to_thread.run_sync(evaluate_cell, self._conn, grid_path, coords)
            grounding_report = None
            if grounding:
                grounding_report = await self._grounding_locked(("cell", grid_path, coords))
            return currency, grounding_report

    async def gated_read_cell(
        self,
        grid_path: str,
        coords: str,
        *,
        strict: bool,
    ) -> FxtrID | None:
        """The data/handle read gate: serve the cell's value from the same
        lock hold that judged its grounding, or fault — the serve-or-fail
        form of the strict gate on the one channel demand does not flow
        through. The reading cascade's own wantedness is already durable
        (its request edges were written at commitment), so the writer's
        vouching needs no evaluation-time extras; the read itself adds no
        demand edge — data stays the no-demand channel.

        Returns:
            The value id, or ``None`` for an empty cell (the caller
            raises its ordinary missing-cell fault).

        Raises:
            UngroundedSubjectError: ``strict`` and the populated cell is
                not grounded (the reason names the roots).
        """
        async with self._lock:
            value = await anyio.to_thread.run_sync(_read_cell, self._conn, grid_path, coords)
            if value is None or not strict:
                return value
            report = await self._grounding_locked(("cell", grid_path, coords))
            if report is None or report.verdict != "grounded":
                reason = report.reason if report is not None else "no grounding verdict"
                raise UngroundedSubjectError(
                    f"cell {grid_path}{coords} is"
                    f" {report.verdict if report is not None else 'unjudgeable'}: {reason}"
                )
            return value

    # -- entity persistence (the liveness invariant) -------------------------

    async def persist(
        self,
        entity: Entity | EntityRef | FxtrID,
        *,
        from_scope: FxtrScope | None = None,
    ) -> FxtrID:
        """Pin an entity's closure into the entity store; returns its id.

        Pin key = the id string (content-keyed, idempotent). Every recording
        method that takes a value id requires the caller to have persisted
        it — the combined helpers below do both in the correct order.

        Args:
            entity: The value to pin (entities are stored first to learn
                their id).
            from_scope: Scope where ``entity``'s closure is loadable (the
                pin's copy source). ``None`` uses the active scope — the
                scope-discipline user-boundary case, for direct user/test
                callers holding the value in their own context. Library
                callers (the ops engine) pass the scope carrying the
                incoming value explicitly.
        """
        if from_scope is None:
            from_scope = get_active_scope()
        if isinstance(entity, Entity):
            entity = entity.store(scope=from_scope)
        entity_id = entity.entity_id if isinstance(entity, EntityRef) else entity
        await self._namespace.pin(
            entity_id.id_string(), entity_id, overwrite=True, scope=from_scope
        )
        return entity_id

    async def ensure_loadable(self, entity_id: FxtrID) -> FxtrID:
        """Re-attach a persisted id to the entity scope; returns it back.

        The class contract ("ids recorded in the state db stay loadable
        below" the entity scope) holds within the persisting process by
        residue — the persist tracked the closure — but a *resumed*
        session must re-attach from the durable store before a recorded
        id can cross a service boundary (return-packet coverage requires
        the node tracked in an ancestor scope). Serving paths (the ops
        engine's record hits and live reads) call this on every id they
        return; an id already tracked in the entity scope returns
        immediately, and the attach itself is a lazy pin lookup,
        idempotent and copy-free.

        Args:
            entity_id: An id previously pinned via :meth:`persist` (pin
                key = the id string). The liveness invariant guarantees
                this for every state-db-recorded id.

        Raises:
            KeyError: The id was never pinned — an integrity violation
                for anything the state db references.
        """
        # Fast path: already tracked in the entity scope (stored or
        # re-attached earlier in this process) — nothing to do.
        if self.entity_scope.is_tracked(entity_id):
            return entity_id
        # Membership: pin key = id string is persist's contract; retrieve
        # lazily attaches the node to the namespace's own scope and lifts
        # it there (entity_scope IS that scope), making it tracked for
        # every descendant.
        return await self._namespace.retrieve(entity_id.id_string(), into_scope=self.entity_scope)

    # -- the frame projection ---------------------------------------------------

    async def frame_bytes(self, frame_id: FxtrID) -> bytes:
        """Serve one frame's arrow IPC stream bytes, in canonical row order.

        The projection serving path (``notes/dataframes.md``): a cache hit
        returns immediately; a miss rebuilds from the canonical frame
        entity — a *trusting* decode (``load_frame(verify=False)``): every
        frame id recorded in the state db was minted host-side by
        `frame_encoding.build_frame_entity`, so its rows are canonical by
        construction, and the session trusts its own store on read exactly
        as it does for configs and step payloads. Rebuilt bytes populate
        the cache (lazy-only population; writes never populate).

        Args:
            frame_id: A frame entity id previously recorded through this
                session (cell value or task result — the liveness
                invariant guarantees the pin).

        Returns:
            The transport form: IPC stream bytes, canonical row order.

        Raises:
            KeyError: The id was never pinned (an integrity violation).
            ValueError: The pinned entity is not a (well-formed) frame
                entity (the typed load / schema decode refuse it).
        """
        data = self._frames.get(frame_id)
        if data is not None:
            return data
        # Membership: re-attach before loading — recorded ids are pinned
        # (persist's contract), and retrieve lifts the node into the
        # entity scope, which load_frame below reads from.
        await self.ensure_loadable(frame_id)
        # Lazy import: tables is the package's sole eager-polars module;
        # importing it here (first frame served) keeps the core import
        # polars-free (the chunk-2 doctrine — cheap sandbox imports).
        from fxtr_experiment.frame_encoding import load_frame
        from fxtr_experiment_task.tables import frame_to_ipc, make_frame

        schema, rows = await load_frame(frame_id, scope=self.entity_scope, verify=False)
        data = frame_to_ipc(make_frame(schema, rows))
        self._frames.put(frame_id, data)
        return data

    # -- grids ----------------------------------------------------------------

    async def create_grid(
        self,
        key_path: str,
        kind: str,
        config: Entity | FxtrID,
        *,
        rule_refs: tuple[str, ...] = (),
        from_scope: FxtrScope | None = None,
    ) -> None:
        """Insert a grid row (persisting its config entity first).

        Args:
            key_path: The grid's key-path text.
            kind: The grid kind.
            config: The canonical config entity (or its id).
            rule_refs: For task grids: the key paths the stored rule
                addresses (terminal slice paths — the caller derives them
                from the config via `model.rule_reference_paths`),
                inserted into the reverse rule-reference index in the same
                transaction.
            from_scope: Semantics match :meth:`persist`.

        Raises:
            EncodingError: If any key in the path violates the key-charset
                law (`encoding.validate_grid_key` — creation is the one
                enforcement point, so no invalid key ever reaches the
                ``grids`` table or the at-rest export layout).
        """
        for key in decode_key_path(key_path):
            validate_grid_key(key)
        config_id = await self.persist(config, from_scope=from_scope)
        await self._run(
            _create_grid, key_path, parent_key_path(key_path), kind, config_id, rule_refs
        )

    async def get_grid(self, key_path: str) -> tuple[str, FxtrID] | None:
        """Return ``(kind, config_id)`` for a grid, or None if absent."""
        return await self._run(_get_grid, key_path)

    async def list_child_grids(self, parent_path: str) -> tuple[tuple[str, str], ...]:
        """All child grids of a parent: ``(key_path, kind)`` rows."""
        return await self._run(_list_children, parent_path)

    async def delete_grid(self, key_path: str, recursive: bool = False) -> tuple[str, ...]:
        """Delete a grid; cascades sweep each deleted grid's contents.

        ``recursive=False`` is rmdir-style: refuses if child grids exist.
        ``recursive=True`` deletes the grid **and every grid under it**
        (its whole subtree — a task grid's read-only internals included),
        which is the only way to delete a task grid that has run. Either
        way, refuses if any generation in the deleted set is running.

        Returns:
            The *external* referrer grids whose rules address a deleted
            path (their grounding flips with this delete) — referrers
            inside the deleted set are excluded (they are gone too). The
            consequence report, enumerated pre-delete via the reverse
            rule-reference index.

        Raises:
            StateDbError: No grid at ``key_path``; child grids exist
                (non-recursive); or a running generation in the set.
        """
        return await self._run(_delete_grid, key_path, recursive)

    async def load_config(self, config_id: FxtrID) -> "GridConfigEntity":
        """Load a canonical config entity back from the entity store.

        Args:
            config_id: A ``grids.config_id`` (pinned at grid creation).

        Returns:
            The loaded :class:`~fxtr_experiment.encoding.GridConfigEntity`
            (``.config`` is the model object).
        """
        # Membership: config ids are pinned into this namespace by
        # create_grid's persist, so the namespace's own scope retrieves
        # and loads them (no reliance on the caller's active scope).
        ref = await self._namespace.retrieve(
            config_id.id_string(), GridConfigEntity, into_scope=self.entity_scope
        )
        return await ref.load(self.entity_scope)

    # -- cells ------------------------------------------------------------------

    async def write_cell(
        self,
        grid_path: str,
        coords: str,
        value: Entity | EntityRef | FxtrID,
        *,
        from_scope: FxtrScope | None = None,
    ) -> FxtrID:
        """Persist ``value`` then record it at the cell (persist-then-record).

        ``from_scope`` semantics match :meth:`persist`."""
        value_id = await self.persist(value, from_scope=from_scope)
        await self._run(_write_cell, grid_path, coords, value_id)
        return value_id

    async def read_cell(self, grid_path: str, coords: str) -> FxtrID | None:
        """The value id at a cell, or None if the cell is empty."""
        return await self._run(_read_cell, grid_path, coords)

    async def handle_cells_holding(
        self, keys: Sequence[FxtrID]
    ) -> dict[FxtrID, tuple[tuple[str, str], ...]]:
        """The handle cells currently holding each key, one snapshot.

        Presence-class (the binding introspection's grid-side join —
        `user_api.UserCapabilityProto.list_bindings`).

        Args:
            keys: Handle entity ids to look up.

        Returns:
            Key → (grid_path, coords) rows of every handle-grid cell
            whose value is that key, ordered by location; keys held by
            no cell map to ``()``.
        """
        return await self._run(_handle_cells_holding, tuple(keys))

    async def delete_cell(self, grid_path: str, coords: str) -> bool:
        """Remove one populated data/handle cell's value (idempotent).

        Deletion is a user settle event, exactly like an overwrite:
        observers of the deleted subject go stale (missing subject), the
        cell's sourcing record — if any — becomes dangling (preserved;
        intent is never silently dropped), and live handle bindings are
        untouched (bindings are runner config keyed by handle entity, not
        grid state). Marks: the sync body's docstring.

        Returns:
            Whether a value existed.
        """
        return await self._run(_delete_cell, grid_path, coords)

    # -- generations -------------------------------------------------------------

    async def insert_generation(
        self, generation: str, grid_path: str, coords: str, current_fn: FxtrID, executed_fn: FxtrID
    ) -> int:
        """Insert a fresh ``running`` generation row; returns its exec_id.

        Raises:
            StateDbError: If the cell already has a current generation.
        """
        return await self._run(
            _insert_generation, generation, grid_path, coords, current_fn, executed_fn
        )

    async def get_generation(
        self, grid_path: str, coords: str
    ) -> tuple[int, str, str, FxtrID] | None:
        """Return ``(exec_id, generation, status, executed_fn)`` for a cell.

        What went wrong (if errored/interrupted) lives in the failure
        records — see :meth:`load_failures`.
        """
        return await self._run(_get_generation, grid_path, coords)

    async def set_status(self, exec_id: int, status: str) -> None:
        """Update a generation's status (the flip-time record choreography).

        Terminal flips maintain the standing invariant of
        ``notes/at_rest_format.md`` — ``record_id`` non-NULL ⇔ status is
        terminal: under one lock hold, assemble the canonical record *at
        the target status*, re-attach its referenced ids (all pinned by
        the liveness invariant, so the re-attach is total), persist the
        record entity, then update status + ``record_id`` in one
        transaction. A crash before that transaction leaves the row
        running (the startup flip re-covers it); the transaction itself is
        atomic — there is no terminal-without-record window. Flips to
        ``running`` clear ``record_id`` in the same transaction (a running
        generation has no at-rest record). Marks: the sync body's
        docstring.

        Raises:
            StateDbError: No such generation; or (terminal flips) a
                referenced id was never persisted — a liveness-invariant
                violation at the original recording site.
        """
        if status in ("complete", "errored", "interrupted"):
            async with self._lock:
                await self._record_generation_locked(exec_id, status_override=status)
        else:
            await self._run(_set_status, exec_id, status)

    async def _record_generation_locked(
        self,
        exec_id: int,
        status_override: str | None,
        crash_failure: "FailureFact | None" = None,
    ) -> None:
        """The flip-time choreography; the caller holds the session lock.

        ``status_override`` carries the target terminal status while the
        row still says running. ``crash_failure`` is the startup flip's
        synthesized RunnerCrash fact: appended to the assembled record in
        memory and inserted as a failure row in the same final transaction
        as the status/pointer update, so the stored record equals a fresh
        assembly of the post-transaction rows and a crash before that
        transaction leaves nothing behind.
        """
        record = await anyio.to_thread.run_sync(
            _build_generation_record, self._conn, exec_id, status_override
        )
        if crash_failure is not None:
            record = canonical_generation_record(
                dataclasses.replace(record, failures=(*record.failures, crash_failure))
            )
        for referenced in generation_record_ids(record):
            # Membership: every id in a generation's rows was persisted
            # before it was recorded (the liveness invariant), so the
            # re-attach into the entity scope is total — which is what
            # lets the record entity (whose strong refs cover these ids)
            # store into that scope.
            try:
                await self.ensure_loadable(referenced)
            except KeyError as exc:
                raise StateDbError(
                    f"generation record references {referenced.id_string()}, which was"
                    " never persisted — a liveness-invariant violation at the original"
                    " recording site"
                ) from exc
        record_id = await self.persist(
            GenerationRecordEntity(record=record), from_scope=self.entity_scope
        )
        # The one mutating transaction of the choreography — collect and
        # flush its hints here (this path bypasses `_run`).
        with collect_hints() as hints:
            await anyio.to_thread.run_sync(
                _set_status, self._conn, exec_id, record.status, record_id, crash_failure
            )
        self._flush_hints(hints)

    async def record_failure(self, exec_id: int, kind: int, error_class: str, message: str) -> None:
        """Append one failure record to a generation's poisoning record.

        Written at raise time by the raising side (the engine for op
        failures; the executor for exceptions escaping a run with no
        engine-recorded failure — see ``storage.py``'s failures writeup).

        Args:
            exec_id: The poisoned generation.
            kind: ``FAILURE_ERROR`` or ``FAILURE_FAULT`` (from the typed
                exception — ``CellFaultError`` and subclasses are faults).
            error_class: The exception's class name (display only).
            message: Human-readable failure message.
        """
        await self._run(_record_failure, exec_id, kind, error_class, message)

    async def load_failures(self, exec_id: int) -> tuple[tuple[int, str, str], ...]:
        """A generation's failure records, in insertion order.

        Returns:
            Rows of ``(kind, error_class, message)``.
        """
        return await self._run(_load_failures, exec_id)

    async def clear_failures(self, exec_id: int) -> None:
        """Drop a generation's failure records (the executor calls this at
        the start of each new run: failures are evidence about the *last*
        attempt, consumed by its terminal-status derivation — a resumed or
        retried run must be able to complete cleanly)."""
        await self._run(_clear_failures, exec_id)

    async def delete_generation(self, exec_id: int) -> None:
        """Generation turnover: one row delete; cascades sweep its records."""
        await self._run(_delete_generation, exec_id)

    async def record_result(
        self,
        exec_id: int,
        output: str,
        value: "Entity | EntityRef | FxtrID",
        *,
        from_scope: FxtrScope | None = None,
    ) -> FxtrID:
        """Persist ``value`` then record it as a per-output result.

        ``from_scope`` semantics match :meth:`persist`."""
        value_id = await self.persist(value, from_scope=from_scope)
        await self._run(_record_result, exec_id, output, value_id)
        return value_id

    async def get_results(self, exec_id: int) -> dict[str, FxtrID]:
        """The per-output results recorded for a generation."""
        return await self._run(_get_results, exec_id)

    # -- record: subjects / observations / assertions -------------------------------

    async def intern_version(
        self, grid_path: str, coords: str, output: str, fingerprint: FxtrID
    ) -> int:
        """Intern an observed fact; returns its (private) version_id."""
        return await self._run(_intern_version, grid_path, coords, output, fingerprint)

    async def record_observation(self, exec_id: int, version_id: int, kind: int) -> None:
        """Insert one observation edge (caller interned the version)."""
        await self._run(_record_observation, exec_id, version_id, kind)

    async def record_grid_assertion(self, exec_id: int, grid_path: str, config_id: FxtrID) -> None:
        """Record a would-create claim: the generation opened ``grid_path``
        through write authority, proposing ``config_id``.

        Idempotent (INSERT OR IGNORE — the monotone-OR accumulation of
        ``notes/grounding.md``): called on every write-authority open,
        record hits included, as its own generation-level fact — never a
        mutation of an observation row. Interns the (grid, config) version
        itself.
        """
        await self._run(_record_grid_assertion, exec_id, grid_path, config_id)

    async def load_assertions(self, exec_id: int) -> tuple[tuple[str, FxtrID], ...]:
        """One generation's would-create claims.

        Returns:
            Rows of ``(grid_path, config fingerprint)`` — what the engine
            mirrors in memory and the safety pass compares against.
        """
        return await self._run(_load_assertions, exec_id)

    async def record_request_edge(
        self, exec_id: int, grid_path: str, coords: str, kind: int
    ) -> None:
        """Record a request commitment: the generation requested the cell
        at ``(grid_path, coords)`` (``kind``: a `storage.REQ_KIND_*`
        constant — task-output demand or data/handle read).

        Called at record-**miss** time, before the op goes live, so the
        demand fact is durable even if the request never resolves.
        Idempotent (INSERT OR IGNORE): an aborted-then-resumed request
        re-commits the same edge; a record hit implies the edge exists
        from the original miss. Frozen engines never miss, so the safety
        pass never reaches this (and compares the stored set instead —
        ``notes/execution_semantics.md``).
        """
        await self._run(_record_request_edge, exec_id, grid_path, coords, kind)

    async def load_request_edges(self, exec_id: int) -> tuple[tuple[str, str, int], ...]:
        """One generation's request edges.

        Returns:
            Rows of ``(grid_path, coords, kind)`` — what the safety pass
            compares against the record's read-class projection.
        """
        return await self._run(_load_request_edges, exec_id)

    async def load_record(self, generation: str) -> tuple[tuple[str, str, str, FxtrID, int], ...]:
        """The forward record-load query: everything a generation observed.

        Returns:
            Rows of ``(grid_path, coords, output, fingerprint, kind)``.
        """
        return await self._run(_load_record, generation)

    async def observers_of(self, grid_path: str, coords: str) -> tuple[tuple[str, str], ...]:
        """The reverse query: ``(grid_path, coords)`` of observer cells."""
        return await self._run(_observers_of, grid_path, coords)

    # -- checkpoints ---------------------------------------------------------------

    async def put_checkpoint(
        self,
        grid_path: str,
        coords: str,
        exec_id: int,
        step_type_id: FxtrID,
        args: Entity | EntityRef | FxtrID,
        value: Entity | EntityRef | FxtrID,
        *,
        from_scope: FxtrScope | None = None,
    ) -> None:
        """Persist args + value + the step's type entity, then record the
        checkpoint entry.

        ``from_scope`` semantics match :meth:`persist` and must cover the
        type entity too (the extraction contract of
        ``notes/at_rest_format.md``: every recorded id is a fetchable
        root; `define_type` stores type entities into ``GLOBAL_SCOPE``,
        so any caller scope descending from it qualifies)."""
        args_id = await self.persist(args, from_scope=from_scope)
        value_id = await self.persist(value, from_scope=from_scope)
        await self.persist(step_type_id, from_scope=from_scope)
        await self._run(
            _put_checkpoint, grid_path, coords, exec_id, step_type_id, args_id, value_id
        )

    async def record_checkpoint(
        self,
        grid_path: str,
        coords: str,
        exec_id: int,
        step_type_id: FxtrID,
        args_id: FxtrID,
        value_id: FxtrID,
    ) -> None:
        """Record one checkpoint entry (record-only).

        The caller has already persisted ``args_id``, ``value_id``, *and*
        ``step_type_id`` — the engine persists each from the scope that
        actually holds its closure (args and the type entity: the op's
        dispatch scope; value: the per-step scope), which a single
        combined helper couldn't express. Prefer :meth:`put_checkpoint`
        when one source scope covers all three.
        """
        await self._run(
            _put_checkpoint, grid_path, coords, exec_id, step_type_id, args_id, value_id
        )

    async def get_checkpoint(
        self, grid_path: str, coords: str
    ) -> tuple[int, FxtrID, FxtrID, FxtrID] | None:
        """Return ``(exec_id, step_type_id, args_id, value_id)`` or None."""
        return await self._run(_get_checkpoint, grid_path, coords)

    async def load_checkpoints(
        self, exec_id: int
    ) -> tuple[tuple[str, str, FxtrID, FxtrID, FxtrID], ...]:
        """One generation's checkpoint entries (its record's step half).

        Returns:
            Rows of ``(grid_path, coords, step_type_id, args_id,
            value_id)`` — what the engine loads beside the observations
            (resume) and what the safety pass compares against.
        """
        return await self._run(_load_checkpoints, exec_id)

    async def load_step_payload(self, payload_id: FxtrID) -> "FxtrServiceData":
        """Load a stored step payload back to service data.

        Args:
            payload_id: A ``checkpoints.args_id`` / ``value_id`` (pinned
                by ``put_checkpoint``'s persist).

        Returns:
            The decoded payload (see
            `encoding.frozen_cbor_to_service_data` for the canonical
            forms).
        """
        # Membership: payload ids are pinned into this namespace by
        # put_checkpoint's persist, so the namespace's own scope retrieves
        # and loads them (no reliance on the caller's active scope).
        ref = await self._namespace.retrieve(
            payload_id.id_string(), StepPayloadEntity, into_scope=self.entity_scope
        )
        entity = await ref.load(self.entity_scope)
        return frozen_cbor_to_service_data(entity.payload)

    # -- the generation record (the at-rest form; notes/at_rest_format.md) ----------

    async def build_generation_record(self, exec_id: int) -> "GenerationRecord":
        """Assemble one non-running generation's canonical record from its
        rows (one transaction, one snapshot): status/fn ids, results,
        failures (insertion order), the op record, assertions, request
        edges, checkpoints — autoincrement-agnostic, in the canonical
        order of `encoding.canonical_generation_record`.

        Raises:
            StateDbError: No such generation, or its status is not
                terminal (records are an at-rest artifact).
        """
        return await self._run(_build_generation_record, exec_id)

    async def export_snapshot(self) -> "ExportSnapshot":
        """One coherent at-rest read of every exported fact (one locked
        transaction — the layout's snapshot consistency comes from here;
        rendering happens outside the lock). Implementation:
        `export.export_snapshot_body`.

        Raises:
            NotAtRestError: A generation is running (checked atomically
                with the read).
        """
        return await self._run(export_snapshot_body)

    async def apply_import(self, bundle: "ImportBundle", force: bool) -> "ImportReport":
        """The destructive half of an import: one locked transaction
        (refuse-while-running + empty-or-``force`` wipe + every insert +
        the derived rebuilds + the event — `importer.apply_import_body`).
        The caller (`user_service`) has already parsed and verified the
        layout against the entity store (`importer.verify_and_decode`).

        Raises:
            NotAtRestError: A generation is running.
            StateNotEmptyError: State present and ``force`` is False.
        """
        async with self._lock:
            return await anyio.to_thread.run_sync(apply_import_body, self._conn, bundle, force)

    async def list_generation_rows(self) -> tuple[tuple[str, str, str, FxtrID | None], ...]:
        """Every generation row's at-rest identity: ``(grid_path, coords,
        status, record_id)``, in (path, coords) order. ``record_id`` is
        ``None`` exactly for running rows (the flip-time invariant) — the
        export enumerator and the invariant probes read this."""
        return await self._run(_list_generation_rows)

    async def load_generation_record(self, record_id: FxtrID) -> "GenerationRecordEntity":
        """Load a stored generation-record entity back.

        Args:
            record_id: A generation record's entity id (pinned by the
                caller's earlier :meth:`persist` — flip-time persistence
                is the standing invariant).

        Returns:
            The loaded entity (``.record`` is the model object).
        """
        # Membership: record ids are pinned by their persist (the
        # liveness invariant), so the namespace's own scope retrieves and
        # loads them — same rationale as load_config.
        ref = await self._namespace.retrieve(
            record_id.id_string(), GenerationRecordEntity, into_scope=self.entity_scope
        )
        return await ref.load(self.entity_scope)

    # -- user sourcing (editable intent; grounding's root facts) --------------------

    async def upsert_user_sourcing(self, grid_path: str, coords: str, fingerprint: FxtrID) -> None:
        """Upsert one sourcing record (one per subject; the pin replaces).

        ``coords = ''`` pins the grid's config; any other coords pin a
        cell's value. Cell rows enforce the grid-before-cell invariant.

        Marks: nothing yet (grounding stamps arrive with the derived
        predicates; this body gains its "Marks:" line then).

        Raises:
            GridNotSourcedError: A cell row whose grid has no grid-level
                sourcing record (source/adopt the grid first).
        """
        await self._run(_upsert_user_sourcing, grid_path, coords, fingerprint)

    async def revoke_user_sourcing(
        self, grid_path: str, coords: str, *, cascade: bool = False
    ) -> bool:
        """Delete one sourcing record; maintains the grid-before-cell
        invariant on the grid-level form.

        Revoking a grid record (``coords = ''``) refuses while cell-level
        intent for that grid exists — cell sourcing records or targets —
        unless ``cascade`` deletes them in the same transaction (the
        primitive under the user surface's future ``force`` affordance).

        Returns:
            Whether a record existed.

        Raises:
            StateDbError: Grid-level revoke with live dependents and no
                ``cascade``.
        """
        return await self._run(_revoke_user_sourcing, grid_path, coords, cascade)

    async def get_user_sourcing(self, grid_path: str, coords: str) -> FxtrID | None:
        """The pinned fingerprint of one sourcing record, or None."""
        return await self._run(_get_user_sourcing, grid_path, coords)

    async def list_user_sourcing(self) -> tuple[tuple[str, str, FxtrID], ...]:
        """All sourcing records as ``(grid_path, coords, fingerprint)``."""
        return await self._run(_list_user_sourcing)

    # -- reverse rule-reference index ------------------------------------------------

    async def grids_referencing(self, referenced_path: str) -> tuple[str, ...]:
        """Key paths of task grids whose stored rules address
        ``referenced_path`` (terminal slice paths; see `storage.py`)."""
        return await self._run(_grids_referencing, referenced_path)

    # -- targets + events -------------------------------------------------------------

    async def add_target(
        self, slice_id: FxtrID, expected_config: FxtrID, grid_path: str, coords: str
    ) -> None:
        """Insert a target (idempotent — PK OR IGNORE); enforces the
        grid-before-cell invariant.

        Args:
            slice_id: Entity id of the encoded wanted slice.
            expected_config: The wanted grid's expected config id.
            grid_path: Denormalized from the slice (see `storage.py`).
            coords: Denormalized fully-bound cell coords.

        Raises:
            GridNotSourcedError: No grid-level sourcing record for
                ``grid_path``.
        """
        await self._run(_add_target, slice_id, expected_config, grid_path, coords)

    async def remove_target(self, slice_id: FxtrID, expected_config: FxtrID) -> None:
        """Delete a target if present (idempotent)."""
        await self._run(_remove_target, slice_id, expected_config)

    async def list_targets(self) -> tuple[tuple[FxtrID, FxtrID, str, str], ...]:
        """All stored targets as (slice_id, expected_config, grid_path,
        coords) rows — locations from the denormalized target columns."""
        return await self._run(_list_targets)

    async def load_slice_template(self, slice_id: FxtrID) -> "SliceTemplateEntity":
        """Load a target's canonical slice entity back from the entity store.

        Args:
            slice_id: A ``targets.slice_id`` (pinned when the target was
                added — the liveness invariant).
        """
        # Membership: slice ids are pinned by add_target's persist, so the
        # namespace's own scope retrieves and loads them.
        ref = await self._namespace.retrieve(
            slice_id.id_string(), SliceTemplateEntity, into_scope=self.entity_scope
        )
        return await ref.load(self.entity_scope)

    async def target_for_slice(self, slice_id: FxtrID) -> FxtrID | None:
        """The target record's expected_config for a slice identity, if any.

        One record per slice identity is the user-surface invariant
        (`user_api.UserCapabilityProto.add_target`); an arbitrary one is
        returned should legacy duplicates ever exist.
        """
        return await self._run(_target_for_slice, slice_id)

    async def remove_targets_for_slice(self, slice_id: FxtrID) -> None:
        """Delete every target record for a slice identity (idempotent)."""
        await self._run(_remove_targets_for_slice, slice_id)

    # -- listing (presence queries) -------------------------------------------

    async def list_grid_cells(self, grid_path: str) -> tuple[tuple[str, FxtrID], ...]:
        """All nonempty cells of one grid as ``(coords, value_id)`` rows."""
        return await self._run(_list_grid_cells, grid_path)

    async def list_generations(self, grid_path: str) -> tuple[tuple[int, str, str], ...]:
        """All generations of one task grid as ``(exec_id, coords, status)``."""
        return await self._run(_list_generations, grid_path)

    async def clear_generations(self, exec_ids: "Sequence[int]") -> None:
        """Atomically delete a set of generations (turnover; FK cascades).

        One transaction, statuses re-checked inside it: if any listed
        generation is currently ``running`` the whole set is refused and
        nothing is deleted — the user surface's atomic clear cannot be
        half-applied around a concurrent demand.

        Raises:
            StateDbError: A listed generation is running.
        """
        await self._run(_clear_generations, tuple(exec_ids))

    async def append_event(self, kind: str, payload: str | None = None) -> None:
        """Append one passive event-log row (wall-clock UTC timestamp)."""
        await self._run(_append_event, _now_iso(), kind, payload)

    # -- sandboxed bindings (durable, not identity) ----------------------------

    async def add_sandbox_binding(
        self,
        key_id: FxtrID,
        defn: SandboxDefn,
        deps: "Mapping[str, tuple[str, str]]",
        config_json: str | None,
    ) -> None:
        """Insert one sandboxed-binding record with its pinned dependencies.

        One transaction: each dependency cell (name-sorted, the canonical
        order) is read and validated — it must be a populated *handle*
        cell — and its current key id is recorded as the pin; then the
        binding + dep rows are inserted. Bindings are not identity, so
        nothing is marked; a ``("bindings","")`` change hint and a
        ``bind_sandbox`` event row are emitted.

        The caller (the capability) is responsible for cross-class
        exclusivity vs the ephemeral registry (under its bind lock) and
        for persisting ``key_id``'s closure first (liveness). This body
        additionally rejects a key already sandbox-bound (defense in depth
        + the ``key_id`` PK).

        Args:
            key_id: The handle key id to bind.
            defn: The nix flake reference triple.
            deps: name → ``(grid_path, coords)`` of each dependency's
                fully-bound handle cell (absolute path text).
            config_json: The config as canonical JSON text, or ``None``.

        Marks: nothing (bindings are not identity).

        Events: ``("bindings","")`` hint + a ``bind_sandbox`` event row.

        Raises:
            HandleAlreadyBoundError: The key already has a sandboxed
                binding.
            SandboxDependencyError: A dependency cell is missing, empty,
                or not a handle cell.
        """
        await self._run(_add_sandbox_binding, key_id, defn, dict(deps), config_json)

    async def remove_sandbox_binding(self, key_id: FxtrID) -> None:
        """Delete one sandboxed-binding record (dep rows cascade).

        Events: ``("bindings","")`` hint + a ``remove_sandbox_binding``
        event row.

        Raises:
            UnknownBindingError: No sandboxed binding for ``key_id``.
        """
        await self._run(_remove_sandbox_binding, key_id)

    async def has_sandbox_binding(self, key_id: FxtrID) -> bool:
        """Whether a sandboxed binding exists for ``key_id`` (the
        exclusivity check's durable half)."""
        return await self._run(_has_sandbox_binding, key_id)

    async def list_sandbox_bindings(self) -> "tuple[SandboxBindingRow, ...]":
        """All sandboxed bindings, ``key_id``-sorted, deps ``name``-sorted
        (the canonical order for listing / export)."""
        return await self._run(_list_sandbox_bindings)

    async def get_sandbox_binding(self, key_id: FxtrID) -> "SandboxBindingRow | None":
        """One sandboxed-binding record (deps ``name``-sorted), or
        ``None`` — the serving funnel's lookup (`bindings.BindingHost`)."""
        return await self._run(_get_sandbox_binding, key_id)

    async def pin_dependency_cells(
        self, deps: "Mapping[str, tuple[str, str]]"
    ) -> "tuple[SandboxDepRow, ...]":
        """Read + validate binding dependency cells; pin their current keys.

        The bind-time pinning shared by both binding classes
        (`notes/sandboxed_bindings.md`): one snapshot, same validation as
        ``add_sandbox_binding``'s transaction — a populated *handle* cell
        per dependency — but no insert: the *ephemeral* class stores the
        returned pins on its in-memory entry (``bind_handle``).

        Args:
            deps: name → ``(grid_path, coords)`` of each dependency's
                fully-bound handle cell (absolute path text).

        Returns:
            The pinned rows, ``name``-sorted (the canonical order).

        Marks: nothing (bindings are not identity). Events: none (the
        ephemeral class is unlogged runner configuration).

        Raises:
            SandboxDependencyError: A dependency cell is missing, empty,
                or not a handle cell.
        """
        return await self._run(_pin_dependency_cells, dict(deps))


# -- sync bodies (each runs in a worker thread; own transaction) ----------------


def _now_iso() -> str:
    """Wall-clock UTC timestamp in ISO form (failure / event rows)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# -- currency marking (the I1 maintenance half) ----------------------------------
#
# Marking = clearing currency stamps up the observer DAG, inside the
# mutating body's own transaction (that transactional coupling is what
# makes the invariant crash-safe with no extra machinery). The propagation
# prunes at generations that are already unstamped: while a generation is
# unstamped, no *new* observation edge can attach to its outputs without a
# demand first verifying (re-stamping) it, so everything downstream of a
# continuously-unstamped node was already cleared when it lost its stamp.
# Cycles terminate for free — a member reached twice is already clear —
# and each generation pays at most one clear per verify cycle, bounding
# total marking work by verification work. Full writeup + trigger table:
# notes/execution_semantics.md.


def _observers_of_subjects(
    conn: sqlite3.Connection, where: str, params: tuple[object, ...]
) -> list[int]:
    """Observer exec_ids of the subjects matching a ``subject_versions``
    filter (any fingerprint — marking is per location; verification does
    the per-edge fingerprint comparison)."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT o.exec_id FROM subject_versions sv"
            " JOIN observations o ON o.version_id = sv.version_id"
            f" WHERE {where}",
            params,
        )
    ]


def _mark_cell_subjects(conn: sqlite3.Connection, grid_path: str, coords: str) -> None:
    """Mark observers of every subject at one cell (all outputs)."""
    _propagate_clears(
        conn,
        _observers_of_subjects(conn, "sv.grid_path = ? AND sv.coords = ?", (grid_path, coords)),
    )


def _mark_grid_config(conn: sqlite3.Connection, grid_path: str) -> None:
    """Mark observers of a grid's config subject (create/delete events)."""
    _propagate_clears(
        conn,
        _observers_of_subjects(conn, "sv.grid_path = ? AND sv.coords = ''", (grid_path,)),
    )


def _mark_grid_all(conn: sqlite3.Connection, grid_path: str) -> None:
    """Mark observers of *every* subject under one grid (grid deletion)."""
    _propagate_clears(conn, _observers_of_subjects(conn, "sv.grid_path = ?", (grid_path,)))


def _clear_own_stamp(conn: sqlite3.Connection, exec_id: int) -> None:
    """Clear one generation's stamp (no propagation — self-marks only)."""
    conn.execute(
        "UPDATE task_execs SET currency_verdict = NULL, currency_reason = NULL,"
        " currency_as_of = NULL WHERE exec_id = ?",
        (exec_id,),
    )


def _propagate_clears(conn: sqlite3.Connection, frontier: list[int]) -> None:
    """Transitive stamp-clearing with the prune-at-unstamped rule.

    Each round clears the stamps still present in the frontier and
    recurses only from the rows actually cleared (an already-unstamped
    generation's downstream cone was cleared when it lost its stamp).
    Runs inside the caller's transaction.

    Events: ``("cells", grid)`` for every grid whose stamps this walk
    actually clears (a cleared stamp is "a displayed verdict may
    change"); the enclosing session op owns the flush.
    """
    while frontier:
        cleared_cells: list[tuple[str, str]] = []
        to_clear: list[int] = []
        for batch in _in_batches(frontier):
            marks = ",".join("?" * len(batch))
            for exec_id, g, c in conn.execute(
                "SELECT exec_id, grid_path, coords FROM task_execs"
                f" WHERE exec_id IN ({marks}) AND currency_verdict IS NOT NULL",
                batch,
            ).fetchall():
                to_clear.append(exec_id)
                cleared_cells.append((g, c))
                note_change("cells", g)
        for batch in _in_batches(to_clear):
            marks = ",".join("?" * len(batch))
            conn.execute(
                "UPDATE task_execs SET currency_verdict = NULL, currency_reason = NULL,"
                f" currency_as_of = NULL WHERE exec_id IN ({marks})",
                batch,
            )
        next_frontier: set[int] = set()
        for g, c in cleared_cells:
            next_frontier.update(
                _observers_of_subjects(conn, "sv.grid_path = ? AND sv.coords = ?", (g, c))
            )
        frontier = list(next_frontier)


def _in_batches(ids: "Sequence[int]", size: int = 500) -> "list[Sequence[int]]":
    """Chunk ids for bounded ``IN (...)`` parameter lists."""
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema on a fresh db, or verify the version.

    Raises:
        StateDbError: Existing db with a different schema version.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        create_workflow_state_schema(conn)
        return
    (version,) = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if version != str(SCHEMA_VERSION):
        raise StateDbError(f"state db schema version {version} != {SCHEMA_VERSION}")


def _running_exec_ids(conn: sqlite3.Connection) -> tuple[int, ...]:
    """The crashed runs a fresh open must flip (the startup worklist).

    The flip itself is `WorkflowSession.open`'s per-row choreography —
    assemble (RunnerCrash synthesized), persist, then one transaction for
    failure row + status + record pointer — so each crashed generation
    gets the same uniform "why interrupted" answer as every other
    failure, with no terminal-without-record state ever representable.
    """
    return tuple(
        r[0]
        for r in conn.execute("SELECT exec_id FROM task_execs WHERE status='running'").fetchall()
    )


def _create_grid(
    conn: sqlite3.Connection,
    key_path: str,
    parent: str,
    kind: str,
    config_id: FxtrID,
    rule_refs: tuple[str, ...],
) -> None:
    """Insert one grid row (config id already persisted by the caller),
    plus the reverse rule-reference rows for task grids.

    **Atomic create-or-attach**: an existing row with the same kind +
    config is a no-op success (no insert, no marks, no events) — the
    benign form of two concurrent create-or-attach opens racing past
    their pre-checks (e.g. two cells of one task grid both first-opening
    the shared checkpoint grid; each caller's own check-then-create
    spans two transactions, so the loser lands here). A different kind
    or config raises :class:`~fxtr_experiment.errors.
    GridConfigMismatchError`, the same error the callers' pre-checks
    give.

    Marks: observers of the grid's config subject — a re-created grid can
    resurrect (or re-condemn) the observers of its deleted predecessor's
    config, which verification decides by fingerprint.

    Grounding marks: GRID of each *referrer* (their ``grid_dep`` input
    flipped from missing to present) and WANTED of cells targeted at this
    path (attachment may flip); the new grid's own rows are born unstamped
    (`grounding.py` (c)).

    Events: ``("grids", "")`` — the set of grids changed.
    """
    with conn:
        existing = conn.execute(
            "SELECT kind, config_id FROM grids WHERE key_path = ?", (key_path,)
        ).fetchone()
        if existing is not None:
            if existing[0] != kind or _id_of(existing[1]) != config_id:
                raise GridConfigMismatchError(
                    f"{key_path} exists with a different kind or canonical config"
                )
            return  # lost create-or-attach race: attach semantics, no change
        conn.execute(
            "INSERT INTO grids (key_path, parent_path, kind, config_id) VALUES (?, ?, ?, ?)",
            (key_path, parent, kind, _id_blob(config_id)),
        )
        for ref in rule_refs:
            conn.execute(
                "INSERT OR IGNORE INTO grid_refs (referrer_path, referenced_path)" " VALUES (?, ?)",
                (key_path, ref),
            )
        note_change("grids")
        _mark_grid_config(conn, key_path)
        seeds: list[Seed] = [
            ("grid", r[0])
            for r in conn.execute(
                "SELECT referrer_path FROM grid_refs WHERE referenced_path = ?", (key_path,)
            ).fetchall()
        ]
        seeds.extend(
            ("wanted", key_path, r[0])
            for r in conn.execute(
                "SELECT coords FROM targets WHERE grid_path = ?", (key_path,)
            ).fetchall()
        )
        mark_grounding(conn, seeds)


def _get_grid(conn: sqlite3.Connection, key_path: str) -> tuple[str, FxtrID] | None:
    """Fetch (kind, config_id) or None."""
    row = conn.execute(
        "SELECT kind, config_id FROM grids WHERE key_path = ?", (key_path,)
    ).fetchone()
    if row is None:
        return None
    return row[0], _id_of(row[1])


def _list_children(conn: sqlite3.Connection, parent: str) -> tuple[tuple[str, str], ...]:
    """Child (key_path, kind) rows via the parent_path index."""
    return tuple(
        conn.execute(
            "SELECT key_path, kind FROM grids WHERE parent_path = ? ORDER BY key_path",
            (parent,),
        ).fetchall()
    )


def _delete_grid(conn: sqlite3.Connection, key_path: str, recursive: bool) -> tuple[str, ...]:
    """Delete a grid (rmdir-style) or, with ``recursive``, a grid and its
    whole subtree; returns the external referrer grids (the delete's
    grounding consequences, for the user surface's report).

    The deleted set ``D`` is ``key_path`` alone (non-recursive) or
    ``key_path`` + all descendants (recursive, walked over ``parent_path``
    — a task grid's read-only internals are exactly these descendants).

    Marks: observers of *every* subject under each grid in ``D`` (config,
    cells, cells' outputs) — the surviving external observers; the
    deleted grids' own generations vanish with the cascade.

    Grounding marks (enumerated *before* the cascade — `grounding.py`
    (c); the seeds read rows the cascade destroys, so they are captured
    while live and applied after): GRID of each external referrer (the
    ``grid_dep`` input vanishes); GEN of every external reader of any
    subject under any grid in ``D``; the departing-record fan of every
    generation in ``D`` (claims + demand edges vanish, as at turnover).
    Subjects inside ``D`` are excluded (they are being deleted). The
    deleted grids' ``wanted_stamps`` rows are dropped — the locations are
    going away.

    Events: ``("grids", "")`` — the set of grids changed (the marking
    walks add per-grid ``cells`` hints for the surviving observers).

    Raises:
        StateDbError: No grid at ``key_path``; child grids exist
            (non-recursive); or a running generation in ``D``.
    """
    with conn:
        if recursive:
            deleted = [
                r[0]
                for r in conn.execute(
                    "WITH RECURSIVE d(key_path) AS ("
                    "  SELECT key_path FROM grids WHERE key_path = ?"
                    "  UNION ALL"
                    "  SELECT g.key_path FROM grids g JOIN d ON g.parent_path = d.key_path)"
                    " SELECT key_path FROM d",
                    (key_path,),
                ).fetchall()
            ]
        else:
            deleted = [
                r[0]
                for r in conn.execute(
                    "SELECT key_path FROM grids WHERE key_path = ?", (key_path,)
                ).fetchall()
            ]
        if not deleted:
            raise StateDbError(f"no grid at {key_path}")
        if not recursive:
            (children,) = conn.execute(
                "SELECT COUNT(*) FROM grids WHERE parent_path = ?", (key_path,)
            ).fetchone()
            if children:
                raise StateDbError(
                    f"grid {key_path} has {children} child grids;"
                    " delete leaves first, or delete recursively"
                )
        marks = ",".join("?" * len(deleted))
        (running,) = conn.execute(
            f"SELECT COUNT(*) FROM task_execs WHERE grid_path IN ({marks}) AND status='running'",
            deleted,
        ).fetchone()
        if running:
            raise StateDbError(f"grid {key_path} has {running} running generations in its subtree")
        # External referrers only (referrers inside D are being deleted).
        referrers = tuple(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT referrer_path FROM grid_refs"
                f" WHERE referenced_path IN ({marks}) AND referrer_path NOT IN ({marks})"
                " ORDER BY referrer_path",
                deleted + deleted,
            ).fetchall()
        )
        seeds: list[Seed] = [("grid", r) for r in referrers]
        # External readers of any subject under any deleted grid.
        for g, c in conn.execute(
            f"SELECT DISTINCT te.grid_path, te.coords FROM subject_versions sv"
            f" JOIN observations o ON o.version_id = sv.version_id"
            f" JOIN task_execs te ON te.exec_id = o.exec_id"
            f" WHERE sv.grid_path IN ({marks}) AND te.grid_path NOT IN ({marks})",
            deleted + deleted,
        ).fetchall():
            seeds.append(("gen", g, c))
        # The departing fan of every generation in D (read before the cascade).
        for gp, coords in conn.execute(
            f"SELECT grid_path, coords FROM task_execs WHERE grid_path IN ({marks})",
            deleted,
        ).fetchall():
            seeds.extend(would_start_fan_seeds(conn, (gp, coords)))
        conn.execute(f"DELETE FROM grids WHERE key_path IN ({marks})", deleted)
        note_change("grids")
        for g in deleted:
            delete_wanted_stamps_under(conn, g)
            _mark_grid_all(conn, g)
        mark_grounding(conn, seeds)
        return referrers


def _write_cell(conn: sqlite3.Connection, grid_path: str, coords: str, value_id: FxtrID) -> None:
    """Upsert one cell row (conflict semantics are the ops engine's job).

    Marks: observers of the cell's subject, iff the value actually changes
    (an idempotent same-value write changes nothing and marks nothing).

    Grounding marks: DCELL(the cell) on a value change — its current-value
    claim set is evaluated against the new value (`grounding.py` (c); a
    new writer's claim on an unchanged value is marked by the wrote-edge
    insert instead).

    Events: ``("cells", grid)`` iff the value actually changes (an
    idempotent same-value write emits nothing).
    """
    with conn:
        blob = _id_blob(value_id)
        row = conn.execute(
            "SELECT value_id FROM cells WHERE grid_path = ? AND coords = ?",
            (grid_path, coords),
        ).fetchone()
        if row is not None and row[0] == blob:
            return  # idempotent same-value write: no change, no mark
        conn.execute(
            "INSERT OR REPLACE INTO cells (grid_path, coords, value_id) VALUES (?, ?, ?)",
            (grid_path, coords, blob),
        )
        note_change("cells", grid_path)
        _mark_cell_subjects(conn, grid_path, coords)
        mark_grounding(conn, [("dcell", grid_path, coords)])


def _delete_cell(conn: sqlite3.Connection, grid_path: str, coords: str) -> bool:
    """Delete one cell row if present; returns whether it existed.

    Marks: observers of the cell's subject, iff a value existed (the
    deletion settles the subject on "missing" — direct observers derive
    stale; the trigger table's write row generalizes).

    Grounding marks: DCELL(the cell), seeded **before** the row delete —
    the walk needs the still-present row: clearing its stamp is what
    reaches the reader generations transitively (the citer walk), and the
    prune-at-unstamped rule stays sound only while the fact exists (an
    unstamped-but-present fact has no stamped dependents, by P). Deleting
    first would destroy the stamp without the walk, stranding stamped
    readers — the reason `_delete_grid` enumerates its seeds pre-cascade.
    """
    with conn:
        row = conn.execute(
            "SELECT 1 FROM cells WHERE grid_path = ? AND coords = ?", (grid_path, coords)
        ).fetchone()
        if row is None:
            return False  # already empty: no change, no mark
        note_change("cells", grid_path)
        _mark_cell_subjects(conn, grid_path, coords)
        mark_grounding(conn, [("dcell", grid_path, coords)])
        conn.execute("DELETE FROM cells WHERE grid_path = ? AND coords = ?", (grid_path, coords))
        return True


def _read_cell(conn: sqlite3.Connection, grid_path: str, coords: str) -> FxtrID | None:
    """Fetch a cell's value id or None."""
    row = conn.execute(
        "SELECT value_id FROM cells WHERE grid_path = ? AND coords = ?", (grid_path, coords)
    ).fetchone()
    return None if row is None else _id_of(row[0])


def _handle_cells_holding(
    conn: sqlite3.Connection, keys: tuple[FxtrID, ...]
) -> dict[FxtrID, tuple[tuple[str, str], ...]]:
    """Handle-grid cells holding each key (grid kind filters out data
    cells that merely contain the same id as data)."""
    out: dict[FxtrID, list[tuple[str, str]]] = {key: [] for key in keys}
    if keys:
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            "SELECT c.value_id, c.grid_path, c.coords FROM cells c"
            " JOIN grids g ON g.key_path = c.grid_path"
            f" WHERE g.kind = 'handle' AND c.value_id IN ({placeholders})"
            " ORDER BY c.grid_path, c.coords",
            tuple(_id_blob(key) for key in keys),
        ).fetchall()
        for value_blob, grid_path, coords in rows:
            out[_id_of(value_blob)].append((grid_path, coords))
    return {key: tuple(cells) for key, cells in out.items()}


def _insert_generation(
    conn: sqlite3.Connection,
    generation: str,
    grid_path: str,
    coords: str,
    current_fn: FxtrID,
    executed_fn: FxtrID,
) -> int:
    """Insert a fresh running generation; returns its exec_id.

    Marks: nothing — a fresh generation is unstamped by default (NULL
    stamp columns), and observers of the cell's *previous* generation
    were cleared at its turnover (``_delete_generation``).

    Events: ``("cells", grid)`` — a running row appeared.

    Raises:
        StateDbError: The cell already has a current generation (UNIQUE).
    """
    with conn:
        try:
            cur = conn.execute(
                "INSERT INTO task_execs (generation, grid_path, coords, status,"
                " current_fn, executed_fn) VALUES (?, ?, ?, 'running', ?, ?)",
                (generation, grid_path, coords, _id_blob(current_fn), _id_blob(executed_fn)),
            )
        except sqlite3.IntegrityError as exc:
            raise StateDbError(f"cell {grid_path}{coords} already has a generation") from exc
        note_change("cells", grid_path)
        assert cur.lastrowid is not None
        return cur.lastrowid


def _get_generation(
    conn: sqlite3.Connection, grid_path: str, coords: str
) -> tuple[int, str, str, FxtrID] | None:
    """Fetch (exec_id, generation, status, executed_fn) or None."""
    row = conn.execute(
        "SELECT exec_id, generation, status, executed_fn FROM task_execs"
        " WHERE grid_path = ? AND coords = ?",
        (grid_path, coords),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2], _id_of(row[3])


def _set_status(
    conn: sqlite3.Connection,
    exec_id: int,
    status: str,
    record_id: FxtrID | None = None,
    crash_failure: "FailureFact | None" = None,
) -> None:
    """Update status (and the record pointer) for one exec row.

    ``record_id`` carries the generation record on terminal flips (the
    caller — the flip-time choreography — persisted it first); non-terminal
    flips always clear the pointer, keeping the invariant ``record_id``
    non-NULL ⇔ terminal. ``crash_failure`` is the startup flip's
    synthesized RunnerCrash fact, inserted here so failure row + status +
    pointer land in ONE transaction (the record was assembled with the
    fact appended — the stored record always equals a fresh assembly).

    Marks: the generation's own stamp always (a re-run flip to ``running``
    invalidates any stale/unsettled stamp; on terminal flips it is
    vacuous — running generations are unstamped); on a *terminal* flip,
    additionally the observers of the cell's subjects (completion is the
    visibility event for the run's results — result recording itself
    marks nothing).

    Grounding marks: the generation's own grounding stamp on a flip *to*
    running (running generations are never stamped — I-G3); terminal
    flips mark nothing (the predicates are status-blind — see the
    "Execution status" section of ``notes/grounding.md``).

    Events: ``("cells", grid)`` — the cell's displayed status changed.
    """
    with conn:
        if crash_failure is not None:
            conn.execute(
                "INSERT INTO failures (exec_id, kind, error_class, message, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    exec_id,
                    FAILURE_KIND_CODES[crash_failure.kind],
                    crash_failure.error_class,
                    crash_failure.message,
                    crash_failure.ts,
                ),
            )
        conn.execute(
            "UPDATE task_execs SET status = ?, record_id = ? WHERE exec_id = ?",
            (status, None if record_id is None else id_blob(record_id), exec_id),
        )
        row = conn.execute(
            "SELECT grid_path, coords FROM task_execs WHERE exec_id = ?", (exec_id,)
        ).fetchone()
        if row is not None:
            note_change("cells", row[0])
        _clear_own_stamp(conn, exec_id)
        if status == "running":
            clear_gen_grounding_stamp(conn, exec_id)
        if status != "running" and row is not None:
            _mark_cell_subjects(conn, row[0], row[1])


def _record_failure(
    conn: sqlite3.Connection, exec_id: int, kind: int, error_class: str, message: str
) -> None:
    """Append one failure row (insertion order = display order).

    Events: ``("cells", grid)`` — failure rows are inspectable while the
    run is still in flight (the poisoned state is displayed state).
    """
    with conn:
        conn.execute(
            "INSERT INTO failures (exec_id, kind, error_class, message, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (exec_id, kind, error_class, message, _now_iso()),
        )
        row = conn.execute(
            "SELECT grid_path FROM task_execs WHERE exec_id = ?", (exec_id,)
        ).fetchone()
        if row is not None:
            note_change("cells", row[0])


def _load_failures(conn: sqlite3.Connection, exec_id: int) -> tuple[tuple[int, str, str], ...]:
    """Fetch (kind, error_class, message) rows in insertion order."""
    return tuple(
        conn.execute(
            "SELECT kind, error_class, message FROM failures"
            " WHERE exec_id = ? ORDER BY failure_id",
            (exec_id,),
        ).fetchall()
    )


def _clear_failures(conn: sqlite3.Connection, exec_id: int) -> None:
    """Delete one generation's failure rows."""
    with conn:
        conn.execute("DELETE FROM failures WHERE exec_id = ?", (exec_id,))


def _delete_generation(conn: sqlite3.Connection, exec_id: int) -> None:
    """Turnover: delete the exec row; FK cascades sweep its records.

    Marks: observers of the cell's subjects (all outputs) — turnover
    unsettles everything that observed this generation's results.

    Grounding marks: the departing record's fan (WANTED of read_out
    subjects, DCELL of written cells, GRID of asserted grids, GEN of the
    readers of this cell's outputs), enumerated before the delete; the
    cell's own WANTED stamp is deliberately untouched (`grounding.py`
    (c)).

    Events: ``("cells", grid)`` — the cell's generation state vanished.
    """
    with conn:
        row = conn.execute(
            "SELECT grid_path, coords FROM task_execs WHERE exec_id = ?", (exec_id,)
        ).fetchone()
        seeds: list[Seed] = []
        if row is not None:
            seeds = would_start_fan_seeds(conn, (row[0], row[1]))
        conn.execute("DELETE FROM task_execs WHERE exec_id = ?", (exec_id,))
        if row is not None:
            note_change("cells", row[0])
            _mark_cell_subjects(conn, row[0], row[1])
            mark_grounding(conn, seeds)


def _record_result(conn: sqlite3.Connection, exec_id: int, output: str, value_id: FxtrID) -> None:
    """Upsert one per-output result row.

    Marks: nothing — results become visible through the terminal-status
    flip (``_set_status``), which is the marking event; nothing external
    can depend on an in-flight result.
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO task_results (exec_id, output, value_id) VALUES (?, ?, ?)",
            (exec_id, output, _id_blob(value_id)),
        )


def _get_results(conn: sqlite3.Connection, exec_id: int) -> dict[str, FxtrID]:
    """All output → value-id results for a generation."""
    return {
        output: _id_of(blob)
        for output, blob in conn.execute(
            "SELECT output, value_id FROM task_results WHERE exec_id = ?", (exec_id,)
        ).fetchall()
    }


def _intern_version(
    conn: sqlite3.Connection, grid_path: str, coords: str, output: str, fingerprint: FxtrID
) -> int:
    """Select-or-insert a subject version; returns its private id."""
    fp = _id_blob(fingerprint)
    with conn:
        row = conn.execute(
            "SELECT version_id FROM subject_versions WHERE grid_path = ? AND coords = ?"
            " AND output = ? AND fingerprint = ?",
            (grid_path, coords, output, fp),
        ).fetchone()
        if row is not None:
            return row[0]
        cur = conn.execute(
            "INSERT INTO subject_versions (grid_path, coords, output, fingerprint)"
            " VALUES (?, ?, ?, ?)",
            (grid_path, coords, output, fp),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


def _note_generation_activity(conn: sqlite3.Connection, exec_id: int) -> None:
    """Emit a ``cells`` watch hint for the task cell that owns ``exec_id``.

    Every generation-scoped op-record write (observation, request edge,
    checkpoint, grid assertion) calls this so that anything a running
    generation records dirties its OWNING task cell's watch — the
    drawer's generation report then streams incrementally as the run
    progresses instead of refreshing only at generation start/end
    (`notes/event_system.md`). This is a pure watch signal: orthogonal to
    the grounding/currency "Marks:" each write documents, coalesced per
    window and diff-suppressed downstream, so over-emission is harmless.
    A no-op if the generation is gone (turnover raced the note)."""
    row = conn.execute("SELECT grid_path FROM task_execs WHERE exec_id = ?", (exec_id,)).fetchone()
    if row is not None:
        note_change("cells", row[0])


def _record_observation(conn: sqlite3.Connection, exec_id: int, version_id: int, kind: int) -> None:
    """Insert one (exec_id, version_id, kind) edge.

    Marks: nothing — running-record traffic can only affect the recording
    generation's own *currency* verdict, and running generations are never
    stamped (they always derive live), so no cached currency verdict can
    depend on an in-flight record.

    Grounding marks — the asymmetry with currency (`grounding.py` (c)):
    a ``wrote_cell`` edge is a new claim → DCELL(subject). Read
    resolutions mark nothing: the demand fact is the request edge, marked
    at commitment (`_record_request_edge`) — resolution adds no fact
    grounding consults. A ``grid_config`` edge feeds nothing either
    (assertions are the claim).
    """
    with conn:
        conn.execute(
            "INSERT INTO observations (exec_id, version_id, kind) VALUES (?, ?, ?)",
            (exec_id, version_id, kind),
        )
        _note_generation_activity(conn, exec_id)
        if kind == OBS_KIND_WROTE_CELL:
            g, c = conn.execute(
                "SELECT grid_path, coords FROM subject_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            mark_grounding(conn, [("dcell", g, c)])


def _record_request_edge(
    conn: sqlite3.Connection, exec_id: int, grid_path: str, coords: str, kind: int
) -> None:
    """INSERT OR IGNORE one request edge (the commitment-time demand fact).

    Marks: nothing (currency never consults requests — it reads the
    resolved-value record).

    Grounding marks: WANTED(subject cell) on an actual task-output-kind
    insert — the edge is a new ``read_out`` fact feeding ``wanted``
    (`grounding.py` (c)); skipped on the OR IGNORE no-change case. A
    data-kind edge feeds only the requesting generation's own verdict
    (running ⇒ unstamped by I-G3), so it marks nothing.
    """
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO request_edges (exec_id, grid_path, coords, kind)"
            " VALUES (?, ?, ?, ?)",
            (exec_id, grid_path, coords, kind),
        )
        if cur.rowcount > 0 and kind == REQ_KIND_TASK_OUTPUT:
            mark_grounding(conn, [("wanted", grid_path, coords)])
        _note_generation_activity(conn, exec_id)


def _load_request_edges(conn: sqlite3.Connection, exec_id: int) -> tuple[tuple[str, str, int], ...]:
    """Fetch one generation's (grid_path, coords, kind) request edges."""
    return tuple(
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT grid_path, coords, kind FROM request_edges WHERE exec_id = ?",
            (exec_id,),
        ).fetchall()
    )


def _record_grid_assertion(
    conn: sqlite3.Connection, exec_id: int, grid_path: str, config_id: FxtrID
) -> None:
    """Intern the (grid, config) version + INSERT OR IGNORE the claim.

    Marks: nothing (currency never consults assertions).

    Grounding marks: GRID(the grid) — its claim set changed
    (`grounding.py` (c)); skipped when the claim row already existed
    (the OR IGNORE no-change case).
    """
    fp = _id_blob(config_id)
    with conn:
        row = conn.execute(
            "SELECT version_id FROM subject_versions WHERE grid_path = ? AND coords = ''"
            " AND output = '' AND fingerprint = ?",
            (grid_path, fp),
        ).fetchone()
        if row is not None:
            version_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO subject_versions (grid_path, coords, output, fingerprint)"
                " VALUES (?, '', '', ?)",
                (grid_path, fp),
            )
            assert cur.lastrowid is not None
            version_id = cur.lastrowid
        cur = conn.execute(
            "INSERT OR IGNORE INTO grid_assertions (exec_id, version_id) VALUES (?, ?)",
            (exec_id, version_id),
        )
        if cur.rowcount > 0:
            mark_grounding(conn, [("grid", grid_path)])
        _note_generation_activity(conn, exec_id)


def _load_assertions(conn: sqlite3.Connection, exec_id: int) -> tuple[tuple[str, FxtrID], ...]:
    """Fetch one generation's (grid_path, config fingerprint) claims."""
    return tuple(
        (row[0], _id_of(row[1]))
        for row in conn.execute(
            "SELECT sv.grid_path, sv.fingerprint FROM grid_assertions ga"
            " JOIN subject_versions sv ON sv.version_id = ga.version_id"
            " WHERE ga.exec_id = ?",
            (exec_id,),
        ).fetchall()
    )


def _require_grid_sourcing(conn: sqlite3.Connection, grid_path: str) -> None:
    """The grid-before-cell invariant's add-side check (inside the
    caller's transaction).

    Raises:
        GridNotSourcedError: No grid-level sourcing record for the path.
    """
    row = conn.execute(
        "SELECT 1 FROM user_sourcing WHERE grid_path = ? AND coords = ''", (grid_path,)
    ).fetchone()
    if row is None:
        raise GridNotSourcedError(
            f"grid {grid_path} has no user sourcing record (cell-level intent"
            " requires grid-level sourcing — source/adopt the grid first)"
        )


def _upsert_user_sourcing(
    conn: sqlite3.Connection, grid_path: str, coords: str, fingerprint: FxtrID
) -> None:
    """Upsert one sourcing record; cell rows check the invariant.

    Marks: nothing (currency never consults sourcing).

    Grounding marks: GRID or DCELL per row kind — the fact's user claim
    changed (`grounding.py` (c)).

    Events: ``("intent", "")`` iff the pin actually changed (a same-value
    re-upsert — e.g. an idempotent write's re-claim — displays
    identically)."""
    with conn:
        if coords != "":
            _require_grid_sourcing(conn, grid_path)
        existing = conn.execute(
            "SELECT fingerprint FROM user_sourcing WHERE grid_path = ? AND coords = ?",
            (grid_path, coords),
        ).fetchone()
        conn.execute(
            "INSERT INTO user_sourcing (grid_path, coords, fingerprint) VALUES (?, ?, ?)"
            " ON CONFLICT (grid_path, coords) DO UPDATE SET fingerprint = excluded.fingerprint",
            (grid_path, coords, _id_blob(fingerprint)),
        )
        if existing is None or existing[0] != _id_blob(fingerprint):
            note_change("intent")
        seed: Seed = ("grid", grid_path) if coords == "" else ("dcell", grid_path, coords)
        mark_grounding(conn, [seed])


def _revoke_user_sourcing(
    conn: sqlite3.Connection, grid_path: str, coords: str, cascade: bool
) -> bool:
    """Delete one sourcing record, maintaining the invariant on grid rows.

    Marks: nothing (currency never consults sourcing).

    Grounding marks: GRID or DCELL per row kind (its user claim changed);
    a cascade additionally seeds DCELL for every deleted cell record and
    WANTED for every deleted target — enumerated before the deletes
    (`grounding.py` (c)).

    Events: ``("intent", "")`` iff any record (or cascaded target) was
    actually removed."""
    with conn:
        seeds: list[Seed] = []
        if coords == "":
            cell_rows = conn.execute(
                "SELECT coords FROM user_sourcing WHERE grid_path = ? AND coords != ''",
                (grid_path,),
            ).fetchall()
            target_rows = conn.execute(
                "SELECT coords FROM targets WHERE grid_path = ?", (grid_path,)
            ).fetchall()
            if cell_rows or target_rows:
                if not cascade:
                    raise StateDbError(
                        f"grid {grid_path} has {len(cell_rows)} cell sourcing records"
                        f" and {len(target_rows)} targets; revoke them first or pass"
                        " cascade (the grid-before-cell invariant is maintained,"
                        " not just checked at add time)"
                    )
                seeds.extend(("dcell", grid_path, c) for (c,) in cell_rows)
                seeds.extend(("wanted", grid_path, c) for (c,) in target_rows)
                conn.execute(
                    "DELETE FROM user_sourcing WHERE grid_path = ? AND coords != ''",
                    (grid_path,),
                )
                conn.execute("DELETE FROM targets WHERE grid_path = ?", (grid_path,))
        cur = conn.execute(
            "DELETE FROM user_sourcing WHERE grid_path = ? AND coords = ?",
            (grid_path, coords),
        )
        if cur.rowcount > 0:
            seeds.append(("grid", grid_path) if coords == "" else ("dcell", grid_path, coords))
        if seeds:
            note_change("intent")
        mark_grounding(conn, seeds)
        return cur.rowcount > 0


def _get_user_sourcing(conn: sqlite3.Connection, grid_path: str, coords: str) -> FxtrID | None:
    """Fetch one record's pinned fingerprint, or None."""
    row = conn.execute(
        "SELECT fingerprint FROM user_sourcing WHERE grid_path = ? AND coords = ?",
        (grid_path, coords),
    ).fetchone()
    return None if row is None else _id_of(row[0])


def _list_user_sourcing(conn: sqlite3.Connection) -> tuple[tuple[str, str, FxtrID], ...]:
    """All sourcing records."""
    return tuple(
        (row[0], row[1], _id_of(row[2]))
        for row in conn.execute(
            "SELECT grid_path, coords, fingerprint FROM user_sourcing" " ORDER BY grid_path, coords"
        ).fetchall()
    )


def _grids_referencing(conn: sqlite3.Connection, referenced_path: str) -> tuple[str, ...]:
    """Referrer key paths via the grid_refs_by_referenced index."""
    return tuple(
        row[0]
        for row in conn.execute(
            "SELECT referrer_path FROM grid_refs WHERE referenced_path = ?"
            " ORDER BY referrer_path",
            (referenced_path,),
        ).fetchall()
    )


def _load_record(
    conn: sqlite3.Connection, generation: str
) -> tuple[tuple[str, str, str, FxtrID, int], ...]:
    """The forward record-load query (see storage.py's index notes)."""
    return tuple(
        (gp, co, out, _id_of(fp), kind)
        for gp, co, out, fp, kind in conn.execute(
            "SELECT sv.grid_path, sv.coords, sv.output, sv.fingerprint, o.kind"
            " FROM task_execs te"
            " JOIN observations o ON o.exec_id = te.exec_id"
            " JOIN subject_versions sv ON sv.version_id = o.version_id"
            " WHERE te.generation = ?",
            (generation,),
        ).fetchall()
    )


def _observers_of(
    conn: sqlite3.Connection, grid_path: str, coords: str
) -> tuple[tuple[str, str], ...]:
    """The reverse who-observed-this-location query."""
    return tuple(
        conn.execute(
            "SELECT te.grid_path, te.coords FROM subject_versions sv"
            " JOIN observations o ON o.version_id = sv.version_id"
            " JOIN task_execs te ON te.exec_id = o.exec_id"
            " WHERE sv.grid_path = ? AND sv.coords = ?",
            (grid_path, coords),
        ).fetchall()
    )


def _put_checkpoint(
    conn: sqlite3.Connection,
    grid_path: str,
    coords: str,
    exec_id: int,
    step_type_id: FxtrID,
    args_id: FxtrID,
    value_id: FxtrID,
) -> None:
    """Insert one checkpoint entry (ids already persisted by caller).

    Events: ``("cells", checkpoint grid)`` — entries are inspectable
    mid-run through the owner's generation report.
    """
    with conn:
        note_change("cells", grid_path)  # the checkpoint grid's own watch
        _note_generation_activity(conn, exec_id)  # the owning task cell's report
        conn.execute(
            "INSERT INTO checkpoints (grid_path, coords, exec_id, step_type_id, args_id,"
            " value_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                grid_path,
                coords,
                exec_id,
                _id_blob(step_type_id),
                _id_blob(args_id),
                _id_blob(value_id),
            ),
        )


def _load_checkpoints(
    conn: sqlite3.Connection, exec_id: int
) -> tuple[tuple[str, str, FxtrID, FxtrID, FxtrID], ...]:
    """Fetch (grid_path, coords, step_type_id, args_id, value_id) rows."""
    return tuple(
        (row[0], row[1], _id_of(row[2]), _id_of(row[3]), _id_of(row[4]))
        for row in conn.execute(
            "SELECT grid_path, coords, step_type_id, args_id, value_id"
            " FROM checkpoints WHERE exec_id = ?",
            (exec_id,),
        )
    )


def _get_checkpoint(
    conn: sqlite3.Connection, grid_path: str, coords: str
) -> tuple[int, FxtrID, FxtrID, FxtrID] | None:
    """Fetch (exec_id, step_type_id, args_id, value_id) or None."""
    row = conn.execute(
        "SELECT exec_id, step_type_id, args_id, value_id FROM checkpoints"
        " WHERE grid_path = ? AND coords = ?",
        (grid_path, coords),
    ).fetchone()
    if row is None:
        return None
    return row[0], _id_of(row[1]), _id_of(row[2]), _id_of(row[3])


def _add_target(
    conn: sqlite3.Connection,
    slice_id: FxtrID,
    expected_config: FxtrID,
    grid_path: str,
    coords: str,
) -> None:
    """INSERT OR IGNORE one target row (invariant-checked).

    Grounding marks: WANTED(the cell) on an actual insert — a new demand
    root (`grounding.py` (c); idempotent re-adds change nothing).

    Events: ``("intent", "")`` iff actually inserted."""
    with conn:
        _require_grid_sourcing(conn, grid_path)
        cur = conn.execute(
            "INSERT OR IGNORE INTO targets (slice_id, expected_config, grid_path, coords)"
            " VALUES (?, ?, ?, ?)",
            (_id_blob(slice_id), _id_blob(expected_config), grid_path, coords),
        )
        if cur.rowcount > 0:
            note_change("intent")
            mark_grounding(conn, [("wanted", grid_path, coords)])


def _remove_target(conn: sqlite3.Connection, slice_id: FxtrID, expected_config: FxtrID) -> None:
    """DELETE one target row.

    Grounding marks: WANTED of the removed row's cell (a demand root
    vanished).

    Events: ``("intent", "")`` iff a row was removed."""
    with conn:
        rows = conn.execute(
            "SELECT grid_path, coords FROM targets WHERE slice_id = ? AND expected_config = ?",
            (_id_blob(slice_id), _id_blob(expected_config)),
        ).fetchall()
        conn.execute(
            "DELETE FROM targets WHERE slice_id = ? AND expected_config = ?",
            (_id_blob(slice_id), _id_blob(expected_config)),
        )
        if rows:
            note_change("intent")
        mark_grounding(conn, [("wanted", g, c) for g, c in rows])


def _list_targets(conn: sqlite3.Connection) -> tuple[tuple[FxtrID, FxtrID, str, str], ...]:
    """All target rows, ids decoded, denormalized locations included."""
    return tuple(
        (_id_of(a), _id_of(b), g, c)
        for a, b, g, c in conn.execute(
            "SELECT slice_id, expected_config, grid_path, coords FROM targets"
        ).fetchall()
    )


def _has_attached_target(conn: sqlite3.Connection, grid_path: str, coords: str) -> bool:
    """Whether an attached target names the cell (expected == current
    config, via the denormalized location columns)."""
    return (
        conn.execute(
            "SELECT 1 FROM targets t JOIN grids g ON g.key_path = t.grid_path"
            " AND g.config_id = t.expected_config"
            " WHERE t.grid_path = ? AND t.coords = ?",
            (grid_path, coords),
        ).fetchone()
        is not None
    )


def _target_for_slice(conn: sqlite3.Connection, slice_id: FxtrID) -> FxtrID | None:
    """Fetch the expected_config of a slice's target record, or None."""
    row = conn.execute(
        "SELECT expected_config FROM targets WHERE slice_id = ?", (_id_blob(slice_id),)
    ).fetchone()
    return None if row is None else _id_of(row[0])


def _remove_targets_for_slice(conn: sqlite3.Connection, slice_id: FxtrID) -> None:
    """DELETE all target rows for one slice identity.

    Grounding marks: WANTED of each removed row's cell (a demand root
    vanished).

    Events: ``("intent", "")`` iff any row was removed."""
    with conn:
        rows = conn.execute(
            "SELECT grid_path, coords FROM targets WHERE slice_id = ?", (_id_blob(slice_id),)
        ).fetchall()
        conn.execute("DELETE FROM targets WHERE slice_id = ?", (_id_blob(slice_id),))
        if rows:
            note_change("intent")
        mark_grounding(conn, [("wanted", g, c) for g, c in rows])


def _list_grid_cells(conn: sqlite3.Connection, grid_path: str) -> tuple[tuple[str, FxtrID], ...]:
    """All (coords, value_id) cell rows of one grid."""
    return tuple(
        (coords, _id_of(blob))
        for coords, blob in conn.execute(
            "SELECT coords, value_id FROM cells WHERE grid_path = ? ORDER BY coords",
            (grid_path,),
        ).fetchall()
    )


def _list_generations(conn: sqlite3.Connection, grid_path: str) -> tuple[tuple[int, str, str], ...]:
    """All (exec_id, coords, status) generation rows of one task grid."""
    return tuple(
        conn.execute(
            "SELECT exec_id, coords, status FROM task_execs WHERE grid_path = ? ORDER BY coords",
            (grid_path,),
        ).fetchall()
    )


def _clear_generations(conn: sqlite3.Connection, exec_ids: tuple[int, ...]) -> None:
    """Delete a set of exec rows in one guarded transaction (see
    `WorkflowSession.clear_generations` for the atomic-refusal contract).

    Marks: observers of each cleared cell's subjects (turnover, exactly
    like ``_delete_generation``, in the same atomic transaction).

    Grounding marks: each cleared generation's departing-record fan,
    enumerated before the deletes (exactly like ``_delete_generation``).

    Events: ``("cells", grid)`` per cleared cell's grid.
    """
    with conn:
        cells: list[tuple[str, str]] = []
        for exec_id in exec_ids:
            row = conn.execute(
                "SELECT status, grid_path, coords FROM task_execs WHERE exec_id = ?", (exec_id,)
            ).fetchone()
            if row is not None and row[0] == "running":
                raise StateDbError(f"generation {exec_id} is running; clear refused")
            if row is not None:
                cells.append((row[1], row[2]))
        seeds: list[Seed] = []
        for cell in cells:
            seeds.extend(would_start_fan_seeds(conn, cell))
        for exec_id in exec_ids:
            conn.execute("DELETE FROM task_execs WHERE exec_id = ?", (exec_id,))
        for grid_path, coords in cells:
            note_change("cells", grid_path)
            _mark_cell_subjects(conn, grid_path, coords)
        mark_grounding(conn, seeds)


def _append_event(conn: sqlite3.Connection, ts: str, kind: str, payload: str | None) -> None:
    """INSERT one event row."""
    with conn:
        conn.execute("INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)", (ts, kind, payload))


def _pin_dep_cells(
    conn: sqlite3.Connection, deps: dict[str, tuple[str, str]]
) -> list[tuple[str, str, str, FxtrID]]:
    """Read + validate binding dependency cells and pin their current keys.

    The shared bind-time half of both binding classes (``bind_sandbox``'s
    insert body below; ``bind_handle`` via
    `WorkflowSession.pin_dependency_cells`): each cell (name-sorted, the
    canonical order) must be a populated *handle* cell; its current key
    becomes the pin.

    Returns:
        ``(name, grid_path, coords, pinned_key_id)`` per dependency,
        name-sorted.

    Raises:
        SandboxDependencyError: A dependency cell is missing / empty /
            not a handle cell.
    """
    pinned: list[tuple[str, str, str, FxtrID]] = []
    for name in sorted(deps):  # name-sorted: the canonical order
        grid_path, coords = deps[name]
        grid = _get_grid(conn, grid_path)
        if grid is None:
            raise SandboxDependencyError(f"dependency {name!r}: no grid at {grid_path} to pin")
        if grid[0] != "handle":
            raise SandboxDependencyError(
                f"dependency {name!r}: grid {grid_path} is a {grid[0]!r} grid," " not a handle grid"
            )
        value = _read_cell(conn, grid_path, coords)
        if value is None:
            raise SandboxDependencyError(
                f"dependency {name!r}: handle cell {grid_path}{coords} is empty"
                " (populate it before binding — the pin records its current key)"
            )
        pinned.append((name, grid_path, coords, value))
    return pinned


def _pin_dependency_cells(
    conn: sqlite3.Connection, deps: dict[str, tuple[str, str]]
) -> tuple[SandboxDepRow, ...]:
    """One-snapshot read+validate+pin with no insert (see
    `WorkflowSession.pin_dependency_cells`)."""
    with conn:
        return tuple(
            SandboxDepRow(name=n, grid_path=g, coords=c, pinned_key_id=v)
            for n, g, c, v in _pin_dep_cells(conn, deps)
        )


def _get_sandbox_binding(conn: sqlite3.Connection, key_id: FxtrID) -> SandboxBindingRow | None:
    """SELECT one sandboxed binding + its deps (see
    `WorkflowSession.get_sandbox_binding`)."""
    row = conn.execute(
        "SELECT git_commit, flake_attr, flake_path, config_json"
        " FROM sandbox_bindings WHERE key_id = ?",
        (_id_blob(key_id),),
    ).fetchone()
    if row is None:
        return None
    deps = tuple(
        SandboxDepRow(name=d[0], grid_path=d[1], coords=d[2], pinned_key_id=_id_of(d[3]))
        for d in conn.execute(
            "SELECT dep_name, grid_path, coords, pinned_key_id"
            " FROM sandbox_binding_deps WHERE key_id = ? ORDER BY dep_name",
            (_id_blob(key_id),),
        )
    )
    return SandboxBindingRow(
        key_id=key_id,
        defn=SandboxDefn(git_commit=row[0], flake_attr=row[1], flake_path=row[2]),
        config_json=row[3],
        deps=deps,
    )


def _add_sandbox_binding(
    conn: sqlite3.Connection,
    key_id: FxtrID,
    defn: SandboxDefn,
    deps: dict[str, tuple[str, str]],
    config_json: str | None,
) -> None:
    """INSERT one sandboxed binding + its pinned dep rows (see
    `WorkflowSession.add_sandbox_binding`).

    Marks: nothing (bindings are not identity).

    Events: ``("bindings","")`` + a ``bind_sandbox`` event row.

    Raises:
        HandleAlreadyBoundError: The key already has a sandboxed binding.
        SandboxDependencyError: A dependency cell is missing / empty /
            not a handle cell.
    """
    with conn:
        if conn.execute(
            "SELECT 1 FROM sandbox_bindings WHERE key_id = ?", (_id_blob(key_id),)
        ).fetchone():
            raise HandleAlreadyBoundError(
                f"handle {key_id.id_string()} already has a sandboxed binding; remove it first"
            )
        pinned = _pin_dep_cells(conn, deps)
        conn.execute(
            "INSERT INTO sandbox_bindings"
            " (key_id, git_commit, flake_attr, flake_path, config_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (_id_blob(key_id), defn.git_commit, defn.flake_attr, defn.flake_path, config_json),
        )
        for name, grid_path, coords, value in pinned:
            conn.execute(
                "INSERT INTO sandbox_binding_deps"
                " (key_id, dep_name, grid_path, coords, pinned_key_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (_id_blob(key_id), name, grid_path, coords, _id_blob(value)),
            )
        note_change("bindings")
        payload = json.dumps(
            {
                "key": key_id.id_string(),
                "defn": [defn.git_commit, defn.flake_attr, defn.flake_path],
            },
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
            (_now_iso(), "bind_sandbox", payload),
        )


def _remove_sandbox_binding(conn: sqlite3.Connection, key_id: FxtrID) -> None:
    """DELETE one sandboxed binding (dep rows cascade via FK).

    Events: ``("bindings","")`` + a ``remove_sandbox_binding`` event row.

    Raises:
        UnknownBindingError: No sandboxed binding for the key (the DELETE
            matched nothing; the transaction rolls back).
    """
    with conn:
        cur = conn.execute("DELETE FROM sandbox_bindings WHERE key_id = ?", (_id_blob(key_id),))
        if cur.rowcount == 0:
            raise UnknownBindingError(f"no sandboxed binding for handle {key_id.id_string()}")
        note_change("bindings")
        conn.execute(
            "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
            (_now_iso(), "remove_sandbox_binding", json.dumps({"key": key_id.id_string()})),
        )


def _has_sandbox_binding(conn: sqlite3.Connection, key_id: FxtrID) -> bool:
    """Whether a sandboxed binding row exists for the key."""
    return (
        conn.execute(
            "SELECT 1 FROM sandbox_bindings WHERE key_id = ?", (_id_blob(key_id),)
        ).fetchone()
        is not None
    )


def _list_sandbox_bindings(conn: sqlite3.Connection) -> tuple[SandboxBindingRow, ...]:
    """All sandboxed bindings, key-sorted, deps name-sorted."""
    out: list[SandboxBindingRow] = []
    for key_blob, git_commit, flake_attr, flake_path, config_json in conn.execute(
        "SELECT key_id, git_commit, flake_attr, flake_path, config_json"
        " FROM sandbox_bindings ORDER BY key_id"
    ).fetchall():
        deps = tuple(
            SandboxDepRow(
                name=name, grid_path=grid_path, coords=coords, pinned_key_id=_id_of(pinned)
            )
            for name, grid_path, coords, pinned in conn.execute(
                "SELECT dep_name, grid_path, coords, pinned_key_id"
                " FROM sandbox_binding_deps WHERE key_id = ? ORDER BY dep_name",
                (key_blob,),
            ).fetchall()
        )
        out.append(
            SandboxBindingRow(
                key_id=_id_of(key_blob),
                defn=SandboxDefn(
                    git_commit=git_commit, flake_attr=flake_attr, flake_path=flake_path
                ),
                config_json=config_json,
                deps=deps,
            )
        )
    return tuple(out)


# The stored-BLOB id converters live in storage.py (shared with the
# currency module); aliased here for the bodies above.
_id_blob = id_blob
_id_of = id_of


def _build_generation_record(
    conn: sqlite3.Connection, exec_id: int, status_override: str | None = None
) -> GenerationRecord:
    """Assemble the canonical at-rest record from the generation's rows.

    One transaction (the session lock serializes it like every op).
    Failures are read in insertion order (``failure_id`` — the one place
    row order is meaningful); every set-valued field is canonicalized by
    `encoding.canonical_generation_record`, so the result is exactly the
    entity form's in-memory shape regardless of row order. Kind codes
    convert to the name literals at this boundary (the record never holds
    the storage integers). ``status_override`` carries the target terminal
    status when the row still says running — the flip-time choreography
    assembles *before* its status transaction.
    """
    row = conn.execute(
        "SELECT generation, status, current_fn, executed_fn FROM task_execs WHERE exec_id = ?",
        (exec_id,),
    ).fetchone()
    if row is None:
        raise StateDbError(f"no generation with exec_id {exec_id}")
    generation, status, current_fn, executed_fn = row
    if status_override is not None:
        status = status_override
    if status not in ("complete", "errored", "interrupted"):
        raise StateDbError(
            f"generation {generation} is {status!r}: records describe at-rest"
            " (terminal) generations only"
        )
    results = {
        name: id_of(vid)
        for name, vid in conn.execute(
            "SELECT output, value_id FROM task_results WHERE exec_id = ?", (exec_id,)
        ).fetchall()
    }
    failures = tuple(
        FailureFact(kind=FAILURE_KIND_NAMES[k], error_class=cls, message=msg, ts=ts)
        for k, cls, msg, ts in conn.execute(
            "SELECT kind, error_class, message, ts FROM failures WHERE exec_id = ?"
            " ORDER BY failure_id",
            (exec_id,),
        ).fetchall()
    )
    observations = tuple(
        ObservationFact(
            kind=OBS_KIND_NAMES[k], grid_path=g, coords=c, output=o, fingerprint=id_of(fp)
        )
        for k, g, c, o, fp in conn.execute(
            "SELECT o.kind, sv.grid_path, sv.coords, sv.output, sv.fingerprint"
            " FROM observations o JOIN subject_versions sv ON sv.version_id = o.version_id"
            " WHERE o.exec_id = ?",
            (exec_id,),
        ).fetchall()
    )
    assertions = tuple(
        AssertionFact(grid_path=g, config_id=id_of(fp))
        for g, fp in conn.execute(
            "SELECT sv.grid_path, sv.fingerprint"
            " FROM grid_assertions ga JOIN subject_versions sv ON sv.version_id = ga.version_id"
            " WHERE ga.exec_id = ?",
            (exec_id,),
        ).fetchall()
    )
    request_edges = tuple(
        RequestFact(kind=REQ_KIND_NAMES[k], grid_path=g, coords=c)
        for g, c, k in conn.execute(
            "SELECT grid_path, coords, kind FROM request_edges WHERE exec_id = ?", (exec_id,)
        ).fetchall()
    )
    checkpoints = tuple(
        CheckpointFact(
            grid_path=g,
            coords=c,
            step_type_id=id_of(st),
            args_id=id_of(a),
            value_id=id_of(v),
        )
        for g, c, st, a, v in conn.execute(
            "SELECT grid_path, coords, step_type_id, args_id, value_id"
            " FROM checkpoints WHERE exec_id = ?",
            (exec_id,),
        ).fetchall()
    )
    return canonical_generation_record(
        GenerationRecord(
            generation=generation,
            status=cast(TerminalStatus, status),
            current_fn=id_of(current_fn),
            executed_fn=id_of(executed_fn),
            results=frozendict(results),
            failures=failures,
            observations=observations,
            assertions=assertions,
            request_edges=request_edges,
            checkpoints=checkpoints,
        )
    )


def _list_generation_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str, str, FxtrID | None], ...]:
    """Every generation row's (grid_path, coords, status, record_id)."""
    return tuple(
        (g, c, s, None if rid is None else id_of(rid))
        for g, c, s, rid in conn.execute(
            "SELECT grid_path, coords, status, record_id FROM task_execs"
            " ORDER BY grid_path, coords"
        ).fetchall()
    )
