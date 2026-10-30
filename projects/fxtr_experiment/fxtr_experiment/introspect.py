"""Dashboard introspection queries, store-internal to `state_store.py`.

The sync enumeration bodies (each runs inside the caller's transaction)
and session-level row types behind the monitoring surface of
``notes/monitoring_ui.md``: the whole-namespace grid summary
(:meth:`~fxtr_experiment.state_store.WorkflowSession.summarize`), one
generation's debug detail — observations with a per-edge current-match
flag, failures, checkpoint entries, unanswered requests
(:meth:`~fxtr_experiment.state_store.WorkflowSession.generation_report`)
— and the reverse-observer enumeration
(:meth:`~fxtr_experiment.state_store.WorkflowSession.list_observers`).

Everything here is presence-class introspection: nothing records, nothing
downstream can depend on it, and locations are absolute (key-path text +
flattened-coords text — root-frame transparency). Like `lint.py`, this
module owns the SQL and the row types; verdict derivation (currency /
grounding) is composed by the session under its lock, which is what makes
each report a single-snapshot fact.

**The per-edge current-match flag** (:attr:`ObservationDetail.current`)
is the itemized form of currency's *direct* staleness check — recorded
fingerprint vs the subject's current settled value, one verdict per edge,
no recursion: ``match`` (the subject currently holds exactly the recorded
value), ``changed`` (it settled on a different value — the stale cause),
``missing`` (no settled value: a deleted cell/grid, or a task output
whose cell has no complete generation). Support-transitive judgement
stays with `currency.py`; this flag answers "which input moved" for the
UI's generation view.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, Mapping

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment.encoding import decode_key_path, encode_key_path
from fxtr_experiment.lint import AttachmentStatus
from fxtr_experiment_task.model import (
    FailureKind,
    GridConfig,
    KeepAxis,
    ObservationKind,
    Param,
    RequestKind,
    SliceBinding,
    SliceMode,
    SliceTemplate,
    TaskGridConfig,
)
from fxtr_experiment.storage import (
    FAILURE_KIND_NAMES,
    OBS_KIND_CELL_VALUE,
    OBS_KIND_GRID_CONFIG,
    OBS_KIND_NAMES,
    OBS_KIND_WROTE_CELL,
    REQ_KIND_NAMES,
    id_of,
)

CurrentMatch = Literal["match", "changed", "missing"]
"""One observation edge's direct check against the live world: the subject
currently holds the recorded value (``match``), settled on a different one
(``changed``), or has no settled value at all (``missing`` — deleted
cell/grid, or a task output with no complete generation)."""


@dataclass(frozen=True)
class GridSummaryRow:
    """One grid's summary at a session snapshot (session-level; the user
    surface maps to `user_api.GridSummary`, decoding axes from the config
    and re-attaching served ids).

    Population counts by kind: task grids count generations (split by
    status; a cell with no generation is no fact — the open-map
    principle), checkpoint grids count entries, leaf grids count populated
    cells, and subtree grids report zero (their cells materialize
    implicitly through descendants; the UI aggregates rollups up the
    tree). Currency tallies cover task grids only and are derived by the
    session's force-evaluation pass (the stamps are the memo).

    Attributes:
        path: The grid's key-path text.
        kind: The grid's kind (a `view_spec.GridKind` literal).
        config_id: The canonical config entity id (NOT re-attached — the
            session return is in-process; the wire layer re-attaches).
        grounding_verdict: The grid's grounding verdict.
        grounding_reason: Its diagnosis; ``None`` iff grounded.
        sourcing: The grid-level sourcing record's attachment status, or
            ``None`` when no record exists.
        populated: Total populated cells / generations / entries (see
            above).
        n_pending: Generations by status (task grids; zero elsewhere).
        n_running: Ditto.
        n_complete: Ditto.
        n_errored: Ditto.
        n_interrupted: Ditto.
        n_stale: Task cells whose current generation derived stale.
        n_unsettled: Ditto, unsettled.
        targets_attached: Target records on this grid pinning its current
            config.
        targets_dangling: Target records on this grid pinning a different
            config (``dangling_mismatched``; records for missing grids
            appear in ``list_targets``/``lint``, never here — this
            summary enumerates grids that exist).
    """

    path: str
    kind: str
    config_id: FxtrID
    grounding_verdict: str | None
    grounding_reason: str | None
    sourcing: AttachmentStatus | None
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
class _GridBase:
    """One grid's SQL-side summary half (pre-verdict; internal): the
    session's summarize composes this with the currency tallies and the
    grounding verdict into :class:`GridSummaryRow`."""

    path: str
    kind: str
    config_id: FxtrID
    sourcing: AttachmentStatus | None
    populated: int
    n_pending: int
    n_running: int
    n_complete: int
    n_errored: int
    n_interrupted: int
    targets_attached: int
    targets_dangling: int


def collect_grid_bases(conn: sqlite3.Connection) -> tuple[_GridBase, ...]:
    """Enumerate every grid with its SQL-side counts, in key-path order.

    One pass of grouped aggregates (cells / generations-by-status /
    checkpoint entries / targets), joined in Python; the sourcing status
    compares the grid-level record's pin against the live config. Runs in
    the caller's transaction.
    """
    cell_counts = dict(
        conn.execute("SELECT grid_path, COUNT(*) FROM cells GROUP BY grid_path").fetchall()
    )
    ckpt_counts = dict(
        conn.execute("SELECT grid_path, COUNT(*) FROM checkpoints GROUP BY grid_path").fetchall()
    )
    status_counts: dict[str, dict[str, int]] = {}
    for grid_path, status, count in conn.execute(
        "SELECT grid_path, status, COUNT(*) FROM task_execs GROUP BY grid_path, status"
    ).fetchall():
        status_counts.setdefault(grid_path, {})[status] = count
    sourcing_pins = dict(
        conn.execute(
            "SELECT grid_path, fingerprint FROM user_sourcing WHERE coords = ''"
        ).fetchall()
    )
    target_counts: dict[str, dict[bytes, int]] = {}
    for grid_path, expected, count in conn.execute(
        "SELECT grid_path, expected_config, COUNT(*) FROM targets"
        " GROUP BY grid_path, expected_config"
    ).fetchall():
        target_counts.setdefault(grid_path, {})[expected] = count
    rows = []
    for path, kind, config_blob in conn.execute(
        "SELECT key_path, kind, config_id FROM grids ORDER BY key_path"
    ).fetchall():
        by_status = status_counts.get(path, {})
        if kind == "task":
            populated = sum(by_status.values())
        elif kind == "checkpoint":
            populated = ckpt_counts.get(path, 0)
        elif kind == "subtree":
            populated = 0
        else:
            populated = cell_counts.get(path, 0)
        pin = sourcing_pins.get(path)
        sourcing: AttachmentStatus | None = (
            None if pin is None else "attached" if pin == config_blob else "dangling_mismatched"
        )
        targets_attached = 0
        targets_dangling = 0
        for expected, count in target_counts.get(path, {}).items():
            if expected == config_blob:
                targets_attached += count
            else:
                targets_dangling += count
        rows.append(
            _GridBase(
                path=path,
                kind=kind,
                config_id=id_of(config_blob),
                sourcing=sourcing,
                populated=populated,
                n_pending=by_status.get("pending", 0),
                n_running=by_status.get("running", 0),
                n_complete=by_status.get("complete", 0),
                n_errored=by_status.get("errored", 0),
                n_interrupted=by_status.get("interrupted", 0),
                targets_attached=targets_attached,
                targets_dangling=targets_dangling,
            )
        )
    return tuple(rows)


def task_cell_locations(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """Every task cell with a current generation, in (path, coords) order —
    the session's currency force-evaluation pass iterates these."""
    return tuple(
        conn.execute("SELECT grid_path, coords FROM task_execs ORDER BY grid_path, coords")
    )


@dataclass(frozen=True)
class ObservationDetail:
    """One recorded observation edge with its direct current-match check.

    Attributes:
        subject_path: The observed grid's key-path text.
        subject_coords: The observed cell's flattened-coords text; ``''``
            when the subject is the grid itself (``grid_config``).
        subject_output: The observed task-output name; ``''`` otherwise.
        kind: The relation (`model.ObservationKind`).
        fingerprint: The recorded value id (NOT re-attached — the wire
            layer re-attaches).
        current: The direct check (see :data:`CurrentMatch`).
    """

    subject_path: str
    subject_coords: str
    subject_output: str
    kind: ObservationKind
    fingerprint: FxtrID
    current: CurrentMatch


@dataclass(frozen=True)
class FailureDetail:
    """One failure record (raise-side; insertion order is display order).

    Attributes:
        kind: The errored/interrupted split (`model.FailureKind`).
        error_class: The raised exception's class name (display only).
        message: The failure message.
        ts: Wall-clock timestamp (ISO).
    """

    kind: FailureKind
    error_class: str
    message: str
    ts: str


@dataclass(frozen=True)
class CheckpointDetail:
    """One durable step entry of the generation (entries exist only for
    completed step invocations — an in-flight step has no row).

    Attributes:
        grid_path: The checkpoint grid's key-path text.
        coords: The entry's flattened-coords text.
        step_type_id: The recorded step-fn identity (NOT re-attached).
        args_id: The entity-encoded invocation payload id (ditto).
        value_id: The recorded result id (ditto).
    """

    grid_path: str
    coords: str
    step_type_id: FxtrID
    args_id: FxtrID
    value_id: FxtrID


@dataclass(frozen=True)
class RequestDetail:
    """One asked-but-unanswered request edge (asked at commitment, no
    observation resolved it) — for interrupted generations this names
    what the run is blocked on.

    Attributes:
        grid_path: The requested grid's key-path text.
        coords: The requested cell's flattened-coords text.
        kind: The subject's output-ness (`model.RequestKind`).
    """

    grid_path: str
    coords: str
    kind: RequestKind


@dataclass(frozen=True)
class GenerationDetail:
    """One task cell's current generation, itemized (session-level; the
    user surface maps to `user_api.GenerationReport`, re-attaching ids).

    Attributes:
        generation: The public generation id.
        status: Its execution status.
        executed_fn: The task-fn handle id that ran (NOT re-attached).
        failures: The failure records, insertion order.
        observations: The op record with per-edge current-match flags,
            in (subject, output) order.
        unanswered_requests: Request edges with no resolving observation.
        checkpoints: The recorded step entries, in (grid, coords) order.
    """

    generation: str
    status: str
    executed_fn: FxtrID
    failures: tuple[FailureDetail, ...]
    observations: tuple[ObservationDetail, ...]
    unanswered_requests: tuple[RequestDetail, ...]
    checkpoints: tuple[CheckpointDetail, ...]


def generation_detail(
    conn: sqlite3.Connection, grid_path: str, coords: str
) -> GenerationDetail | None:
    """One cell's current generation, fully itemized, at one snapshot.

    The observation rows join each subject's *current* contents — the
    cell value, the grid config, and (for output subjects) the subject
    cell's current complete generation's result — so the per-edge
    ``current`` flag is derived in the same transaction. Runs in the
    caller's transaction.

    Returns:
        The detail, or ``None`` when the cell has no current generation.
    """
    row = conn.execute(
        "SELECT exec_id, generation, status, executed_fn FROM task_execs"
        " WHERE grid_path = ? AND coords = ?",
        (grid_path, coords),
    ).fetchone()
    if row is None:
        return None
    exec_id, generation, status, executed_fn = row
    failures = tuple(
        FailureDetail(kind=FAILURE_KIND_NAMES[kind], error_class=cls, message=msg, ts=ts)
        for kind, cls, msg, ts in conn.execute(
            "SELECT kind, error_class, message, ts FROM failures"
            " WHERE exec_id = ? ORDER BY failure_id",
            (exec_id,),
        ).fetchall()
    )
    observations = []
    for (
        sub_path,
        sub_coords,
        output,
        fp,
        kind,
        cur_cell,
        cur_config,
        sub_status,
        sub_result,
    ) in conn.execute(
        """
            SELECT sv.grid_path, sv.coords, sv.output, sv.fingerprint, o.kind,
                   cells.value_id, grids.config_id, te2.status, tr.value_id
              FROM observations o
              JOIN subject_versions sv ON sv.version_id = o.version_id
              LEFT JOIN cells ON cells.grid_path = sv.grid_path
                             AND cells.coords = sv.coords
              LEFT JOIN grids ON grids.key_path = sv.grid_path
              LEFT JOIN task_execs te2 ON sv.output != ''
                             AND te2.grid_path = sv.grid_path
                             AND te2.coords = sv.coords
              LEFT JOIN task_results tr ON tr.exec_id = te2.exec_id
                             AND tr.output = sv.output
              WHERE o.exec_id = ?
              ORDER BY sv.grid_path, sv.coords, sv.output
            """,
        (exec_id,),
    ).fetchall():
        # The direct check, per subject shape (mirrors currency's stale
        # logic edge by edge; no recursion — see the module docstring).
        if kind == OBS_KIND_GRID_CONFIG:
            live = cur_config
        elif output:
            live = sub_result if sub_status == "complete" else None
        else:
            live = cur_cell
        current: CurrentMatch = "missing" if live is None else "match" if live == fp else "changed"
        observations.append(
            ObservationDetail(
                subject_path=sub_path,
                subject_coords=sub_coords,
                subject_output=output,
                kind=OBS_KIND_NAMES[kind],
                fingerprint=id_of(fp),
                current=current,
            )
        )
    unanswered = tuple(
        RequestDetail(grid_path=g, coords=c, kind=REQ_KIND_NAMES[kind])
        for g, c, kind in conn.execute(
            """
            SELECT r.grid_path, r.coords, r.kind FROM request_edges r
              WHERE r.exec_id = ?
                AND NOT EXISTS (
                    SELECT 1 FROM observations o
                      JOIN subject_versions sv ON sv.version_id = o.version_id
                      WHERE o.exec_id = r.exec_id
                        AND sv.grid_path = r.grid_path AND sv.coords = r.coords
                )
              ORDER BY r.grid_path, r.coords
            """,
            (exec_id,),
        ).fetchall()
    )
    checkpoints = tuple(
        CheckpointDetail(
            grid_path=g,
            coords=c,
            step_type_id=id_of(step_type),
            args_id=id_of(args),
            value_id=id_of(value),
        )
        for g, c, step_type, args, value in conn.execute(
            "SELECT grid_path, coords, step_type_id, args_id, value_id FROM checkpoints"
            " WHERE exec_id = ? ORDER BY grid_path, coords",
            (exec_id,),
        ).fetchall()
    )
    return GenerationDetail(
        generation=generation,
        status=status,
        executed_fn=id_of(executed_fn),
        failures=failures,
        observations=tuple(observations),
        unanswered_requests=unanswered,
        checkpoints=checkpoints,
    )


@dataclass(frozen=True)
class ObserverRow:
    """One observer cell of a subject (session-level; the user surface
    maps to `user_api.ObserverInfo`).

    Attributes:
        grid_path: The observer task grid's key-path text.
        coords: The observer cell's flattened-coords text.
        status: Its current generation's execution status.
        currency: Its derived currency (from the session's evaluation
            pass under the same lock hold).
    """

    grid_path: str
    coords: str
    status: str
    currency: str | None


def observer_cells(
    conn: sqlite3.Connection, grid_path: str, coords: str, output: str | None
) -> tuple[tuple[str, str], ...]:
    """Distinct observer cells of one subject location, in (path, coords)
    order — location → versions (the identity index) → edges → observer
    generations. ``output`` narrows to one task-output subject; ``None``
    covers every subject at the cell. Runs in the caller's transaction.
    """
    where = "sv.grid_path = ? AND sv.coords = ?"
    params: tuple[object, ...] = (grid_path, coords)
    if output is not None:
        where += " AND sv.output = ?"
        params = (*params, output)
    return tuple(
        conn.execute(
            "SELECT DISTINCT te.grid_path, te.coords FROM subject_versions sv"
            " JOIN observations o ON o.version_id = sv.version_id"
            " JOIN task_execs te ON te.exec_id = o.exec_id"
            f" WHERE {where} ORDER BY te.grid_path, te.coords",
            params,
        ).fetchall()
    )


# -- the grid graph (the plate view's relational overlay) ------------------------
#
# Two halves (`notes/plate_view.md`): the *edges* are grid-level aggregates of
# the per-generation records (SQL, here); the *structure* — input grants and
# `Param` axis correspondences — is derived from the decoded stored rules
# (`build_graph_structure`, pure over configs the impl loads). Nodes, axes,
# containment, and status are NOT here — the dashboard takes those from the
# workflow summary and joins this overlay.

GraphEdgeKind = Literal["read", "wrote_cell", "grid_assertion"]
"""One grid-level relationship in the graph overlay, collapsed from the
per-generation records: a current generation in the observer grid read a cell
of the subject (``read`` — a ``cell_value`` observation), wrote a cell of it
(``wrote_cell``), or asserted (would-create) it (``grid_assertion`` — a
``grid_assertions`` claim). ``grid_config`` observations are deliberately NOT
surfaced as edges — a task observing a grid's config is redundant with the
creation edge (for grids it opens with write authority) or with the actual
cell reads/writes, so it would only add noise (see ``notes/plate_view.md``)."""

_OBS_EDGE_KIND: dict[int, GraphEdgeKind] = {
    OBS_KIND_CELL_VALUE: "read",
    OBS_KIND_WROTE_CELL: "wrote_cell",
}


@dataclass(frozen=True)
class GraphEdgeRow:
    """One grid-pair relationship edge (session-level; the user surface maps
    to `user_api.GraphEdge`).

    Aggregated over **current** generations by construction: the joins go
    through ``task_execs`` (which holds the current generation per cell), and
    a superseded generation's observation / assertion rows cascade away at
    turnover.

    Attributes:
        observer_path: The task grid whose generations produced the record.
        subject_path: The read / written / asserted grid.
        kind: The relationship (see :data:`GraphEdgeKind`).
        count: How many cell-level records collapsed into this grid pair.
    """

    observer_path: str
    subject_path: str
    kind: GraphEdgeKind
    count: int


def collect_graph_edges(conn: sqlite3.Connection) -> tuple[GraphEdgeRow, ...]:
    """Every grid-pair relationship edge, aggregated to the grid level.

    Two grouped passes (observation kinds; would-create assertions), each
    joining the subject location (``subject_versions``) and the observer grid
    (``task_execs``). Runs in the caller's transaction.
    """
    rows: list[GraphEdgeRow] = []
    for observer, subject, kind, count in conn.execute(
        """
        SELECT te.grid_path, sv.grid_path, o.kind, COUNT(*)
          FROM observations o
          JOIN subject_versions sv ON sv.version_id = o.version_id
          JOIN task_execs te ON te.exec_id = o.exec_id
         GROUP BY te.grid_path, sv.grid_path, o.kind
        """
    ).fetchall():
        edge_kind = _OBS_EDGE_KIND.get(kind)
        if edge_kind is None:
            continue  # grid_config observations are not surfaced as edges
        rows.append(
            GraphEdgeRow(
                observer_path=observer,
                subject_path=subject,
                kind=edge_kind,
                count=count,
            )
        )
    for observer, subject, count in conn.execute(
        """
        SELECT te.grid_path, sv.grid_path, COUNT(*)
          FROM grid_assertions ga
          JOIN subject_versions sv ON sv.version_id = ga.version_id
          JOIN task_execs te ON te.exec_id = ga.exec_id
         GROUP BY te.grid_path, sv.grid_path
        """
    ).fetchall():
        rows.append(
            GraphEdgeRow(
                observer_path=observer,
                subject_path=subject,
                kind="grid_assertion",
                count=count,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class GridConfigRow:
    """One grid's stored kind + canonical config id (session-level) — the
    minimal enumeration the structural overlay decodes.

    Attributes:
        path: The grid's key-path text.
        kind: The grid's kind.
        config_id: The canonical config entity id (NOT re-attached — the
            structural build loads it through the session's own store).
    """

    path: str
    kind: str
    config_id: FxtrID


def collect_grid_configs(conn: sqlite3.Connection) -> tuple[GridConfigRow, ...]:
    """Every grid's ``(path, kind, config id)``, in key-path order. Runs in
    the caller's transaction."""
    return tuple(
        GridConfigRow(path=p, kind=k, config_id=id_of(cid))
        for p, k, cid in conn.execute(
            "SELECT key_path, kind, config_id FROM grids ORDER BY key_path"
        ).fetchall()
    )


@dataclass(frozen=True)
class InputGrantRow:
    """One rule-wiring grant edge (session-level; maps to
    `user_api.InputGrantEdge`)."""

    task_path: str
    target_path: str
    arg_name: str
    is_task_fn: bool
    mode: SliceMode
    kept: bool


@dataclass(frozen=True)
class AxisCorrespondenceRow:
    """One provable axis identity from a ``Param`` binding (session-level;
    maps to `user_api.AxisCorrespondence`). Emitted in canonical
    ``(grid, axis)`` order so the overlay diffs stably."""

    grid_a: str
    axis_a: str
    grid_b: str
    axis_b: str


def _flattened_frame(
    path: str, own_axes: Mapping[str, tuple[str, ...]]
) -> tuple[tuple[str, str], ...]:
    """A grid's flattened frame as ``(owning grid path, own-axis name)`` per
    position: each enclosing subtree's own axes in path order, then the
    grid's own (the frame arithmetic of ``view_spec.py``). A position whose
    owning grid is absent from ``own_axes`` (a dangling ancestor) contributes
    nothing."""
    keys = decode_key_path(path)
    frame: list[tuple[str, str]] = []
    for i in range(len(keys) + 1):
        prefix = encode_key_path(keys[:i])
        for name in own_axes.get(prefix, ()):
            frame.append((prefix, name))
    return tuple(frame)


def build_graph_structure(
    configs: Mapping[str, GridConfig],
) -> tuple[tuple[InputGrantRow, ...], tuple[AxisCorrespondenceRow, ...]]:
    """The rule-derived overlay: input grants + ``Param`` axis correspondences.

    Pure over the decoded configs (no db, no lock — the impl loads the
    configs and calls this), so it is unit-testable with hand-built configs.
    Grant-matching (``notes/plate_view.md``): each task rule's ``task_fn``
    slice and every :class:`~fxtr_experiment.model.SliceBinding` input yields
    a grant edge; every :class:`~fxtr_experiment.model.Param` in a slice's
    flattened templates yields a correspondence between the consuming grid's
    flattened position and the target's, each resolved to its owning grid +
    own-axis name (which may be an enclosing subtree — prefix-position
    bindings). Coordinate inputs have no storage target and are skipped; a
    target absent from ``configs`` (a dangling reference) still yields a grant
    edge but no correspondence (its frame is unresolvable).
    """
    own_axes = {path: tuple(a.name for a in cfg.axes) for path, cfg in configs.items()}
    grants: list[InputGrantRow] = []
    corr: set[tuple[str, str, str, str]] = set()
    for path, cfg in configs.items():
        if not isinstance(cfg, TaskGridConfig):
            continue
        task_frame = _flattened_frame(path, own_axes)
        rule = cfg.rule
        wired: list[tuple[str, bool, SliceMode, SliceTemplate]] = [("", True, "read", rule.task_fn)]
        for name, binding in rule.inputs.items():
            if isinstance(binding, SliceBinding):
                wired.append((name, False, binding.mode, binding.slice))
            # CoordBinding: no storage target — no grant edge, no correspondence.
        for arg_name, is_task_fn, mode, slc in wired:
            target_path = encode_key_path(tuple(step.key for step in slc.path))
            flat = [t for step in slc.path for t in step.cell] + list(slc.target_axes)
            grants.append(
                InputGrantRow(
                    task_path=path,
                    target_path=target_path,
                    arg_name=arg_name,
                    is_task_fn=is_task_fn,
                    mode=mode,
                    kept=any(isinstance(t, KeepAxis) for t in flat),
                )
            )
            if target_path not in own_axes:
                continue
            target_frame = _flattened_frame(target_path, own_axes)
            for j, tmpl in enumerate(flat):
                if (
                    isinstance(tmpl, Param)
                    and tmpl.position < len(task_frame)
                    and j < len(target_frame)
                ):
                    a, b = task_frame[tmpl.position], target_frame[j]
                    if a != b:
                        lo, hi = (a, b) if a <= b else (b, a)
                        corr.add((lo[0], lo[1], hi[0], hi[1]))
    corr_rows = tuple(AxisCorrespondenceRow(*c) for c in sorted(corr))
    return tuple(grants), corr_rows
