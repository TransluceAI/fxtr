"""Watch event schemas: the typed wire vocabulary of the watch streams.

One event union per watch family on the user capability
(``notes/event_system.md``; the methods are declared in `user_api.py`,
the emitting engine lives in `watch_engine.py`): every delivery a
subscriber receives is exactly one member of the family's
``*WatchEvent`` union, encoded to wire form by the engine
(sender-encodes, per :mod:`fxtr2.services.subscription_types`'s payload
typing) and mirrored TS-side in the dashboard's ``wire/types.ts``. All
variants are frozen mashumaro-codec'd dataclasses discriminated by a
literal ``kind`` field (the `fxtr2.common.channels` convention).

Shared delivery vocabulary (the contract details live on the watch
methods and in the design note):

- **Batches** (``*Batch``): one recompute pass emits one batch of
  patches — keyed upserts/removes for the keyed collections, whole-value
  replaces for the scalar sub-topics. A subscriber folds patches into
  its mirror in delivery order; the first batch after subscribing is the
  snapshot (the diff from the empty mirror). An EMPTY batch is legal and
  meaningful: it is the settle marker after a ``recomputing``.
- **``recomputing``**: a potentially-slow recompute pass began (cold
  ``summarize`` force-evaluation, ``lint``); the pass's terminal event —
  a batch (possibly empty), or the ``subject_gone`` tombstone when the
  pass discovers the subject deleted — is the settle signal. Quiet
  windows emit nothing, and known-gone passes announce nothing (they
  are cheap probes that publish nothing while the subject stays gone).
- **``subject_gone``**: the watched subject's *grid* no longer exists —
  a tombstone, not silence. The watch stays live; a re-creation at the
  same path resumes with a fresh snapshot batch. Cell/row *emptiness* is
  data, never a tombstone (the inspection channels' never-fault-on-
  emptiness convention): a cleared cell arrives as a row remove or an
  empty report.
- **Log events** (cell watch, task cells): event-native deliveries from
  the runner's run-log buffer — a reset marker when a new run restarts
  the buffer, then appended entries; the entries already buffered at
  subscribe time are delivered right after the first batch.

.. todo::

   Extend the cell watch's runtime-stream vocabulary for task-level
   output channels (`plans/phase4_monitoring_ui/chunk7_task_channels.md`):
   a running cell's channel-reader announcement rides here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment.grounding import GroundingVerdict
from fxtr_experiment_task.model import CoordValue
from fxtr_experiment_task.runner_api import ViewGrant, ViewToken
from fxtr_experiment.user_api import (
    BindingInfo,
    CellInfo,
    CellReport,
    GenerationReport,
    GridGraph,
    GridSummary,
    LintReport,
    ObserverInfo,
    SourcingInfo,
    SourcingStatus,
    TargetInfo,
)

# -- the run-log stream (cell watch, task cells) ---------------------------------

LogEntryKind = Literal[
    "log", "wrote_cell", "read_cell", "demand", "step_started", "step_hit", "step_errored"
]
"""What produced a run-log entry: a task-authored ``log()`` call, or one
of the engine's lifecycle notes (an effectful op the run performed — the
progress trail that needs no task-code cooperation). The step lifecycle
notes (``step_started`` / ``step_hit`` / ``step_errored``) carry a
`StepRef`; a running step is a ``step_started`` with no checkpoint entry
yet, an errored step a ``step_errored`` (the one step outcome with no
checkpoint row — completion is signaled by the checkpoint entry itself,
so there is deliberately no ``step_completed``)."""


@dataclass(frozen=True)
class StepRef:
    """A step's identity: the checkpoint cell it runs into.

    The same ``(grid_path, coords)`` vocabulary the checkpoint entries
    use (`user_api.CheckpointInfo`), so a client keys the run-log's step
    lifecycle notes to checkpoint entries by tuple equality — no string
    parsing.

    Attributes:
        grid_path: The checkpoint grid's key-path text.
        coords: The step's flattened-coords text.
    """

    grid_path: str
    coords: str


@dataclass(frozen=True)
class LogEntry:
    """One run-log line: task-authored or engine-lifecycle.

    Runtime-only provenance of one run's progress (the run-log buffer —
    ``notes/event_system.md``); never persisted, restarted per run.

    Attributes:
        ts: Wall-clock timestamp (ISO form).
        entry_kind: What produced the entry.
        step: The step this entry belongs to (`StepRef`) — the invoking
            step for a task-authored ``log()`` inside a step, or the
            subject step for the ``step_*`` lifecycle notes; ``None`` for
            entries from the task fn's outer level.
        message: The entry text — the ``log()`` argument, or the
            lifecycle note's description.
    """

    ts: str
    entry_kind: LogEntryKind
    step: StepRef | None
    message: str


# -- the workflow watch (watch_workflow) ------------------------------------------


@dataclass(frozen=True)
class SummaryUpsert:
    """Insert-or-replace summary rows, keyed by ``row.path``.

    Attributes:
        rows: The changed :class:`~fxtr_experiment.user_api.GridSummary`
            rows (whole-row replacement per path).
    """

    rows: tuple[GridSummary, ...]
    kind: Literal["summary_upsert"] = "summary_upsert"


@dataclass(frozen=True)
class SummaryRemove:
    """Remove summary rows: these grids no longer exist.

    Attributes:
        paths: The removed grids' key-path texts.
    """

    paths: tuple[str, ...]
    kind: Literal["summary_remove"] = "summary_remove"


@dataclass(frozen=True)
class LintUpdate:
    """Replace the lint report (it changed as a whole).

    Attributes:
        report: The fresh :class:`~fxtr_experiment.user_api.LintReport`.
    """

    report: LintReport
    kind: Literal["lint"] = "lint"


@dataclass(frozen=True)
class TargetsUpdate:
    """Replace the targets list.

    Attributes:
        targets: The fresh ``list_targets`` shape (records round-trip
            into ``demand_target`` verbatim, as ever).
    """

    targets: tuple[TargetInfo, ...]
    kind: Literal["targets"] = "targets"


@dataclass(frozen=True)
class SourcingUpdate:
    """Replace the sourcing-records list.

    Attributes:
        records: The fresh ``list_sourcing`` shape.
    """

    records: tuple[SourcingInfo, ...]
    kind: Literal["sourcing"] = "sourcing"


@dataclass(frozen=True)
class BindingsUpdate:
    """Replace the live-bindings list.

    Attributes:
        bindings: The fresh ``list_bindings`` shape (ephemeral runner
            state, like the query).
    """

    bindings: tuple[BindingInfo, ...]
    kind: Literal["bindings"] = "bindings"


type WorkflowPatch = SummaryUpsert | SummaryRemove | LintUpdate | TargetsUpdate | SourcingUpdate | BindingsUpdate
"""One workflow-mirror patch. Keyed sub-topic: the summary rows (by
path). Scalar sub-topics (whole-value replace): lint, targets, sourcing,
bindings."""


@dataclass(frozen=True)
class WorkflowBatch:
    """One recompute pass's patches (possibly empty — the settle marker).

    Attributes:
        patches: The changed sub-topics, in fold order.
    """

    patches: tuple[WorkflowPatch, ...]
    kind: Literal["batch"] = "batch"


@dataclass(frozen=True)
class WorkflowRecomputing:
    """A slow-flagged workflow recompute began (cold summarize / lint);
    the terminal batch settles it."""

    kind: Literal["recomputing"] = "recomputing"


type WorkflowWatchEvent = WorkflowBatch | WorkflowRecomputing
"""One ``watch_workflow`` delivery."""


# -- the grid watch (watch_cells) --------------------------------------------------


@dataclass(frozen=True)
class GridMeta:
    """The watched grid's header bundle (everything about the grid
    itself, as opposed to its rows).

    Carries the grants the mutation surface needs (`ViewSel`\\ s are
    built from them — ``notes/event_system.md``, grants ride the
    payloads): the grid's own grant plus its parent's token (grid-level
    mutations address parent + key). Token lifetimes align with the
    watch by construction — a capability-mount death breaks the
    subscription, and the resubscribe delivers fresh tokens.

    Attributes:
        path: The grid's key-path text.
        config_id: The canonical config entity (re-attached).
        grounding: The grid's grounding verdict.
        grounding_reason: Its diagnosis; ``None`` iff grounded.
        sourcing: The grid-level sourcing record's attachment status;
            ``None`` when no record exists.
        grant: The grid's own grant, minted through the root-frame walk
            (internals crossings read-only-forced, like ``attach``); its
            axes are the full flattened frame — the cells table's coord
            columns.
        parent_token: The parent subtree's token (grid-level mutations
            address parent + key); ``None`` for the root grid itself.
    """

    path: str
    config_id: FxtrID
    grounding: GroundingVerdict | None
    grounding_reason: str | None
    sourcing: SourcingStatus | None
    grant: ViewGrant
    parent_token: ViewToken | None
    kind: Literal["meta"] = "meta"


@dataclass(frozen=True)
class CellsUpsert:
    """Insert-or-replace cell rows, keyed by ``row.coords``.

    Attributes:
        cells: The changed :class:`~fxtr_experiment.user_api.CellInfo`
            rows (whole-row replacement per coordinate tuple).
    """

    cells: tuple[CellInfo, ...]
    kind: Literal["cells_upsert"] = "cells_upsert"


@dataclass(frozen=True)
class CellsRemove:
    """Remove cell rows: these cells are no longer populated facts.

    Attributes:
        coords: The removed cells' coordinate tuples (the same identity
            vocabulary as ``CellInfo.coords``).
    """

    coords: tuple[tuple[CoordValue, ...], ...]
    kind: Literal["cells_remove"] = "cells_remove"


type CellsPatch = GridMeta | CellsUpsert | CellsRemove
"""One grid-mirror patch: the meta bundle (whole-value replace) or keyed
row upserts/removes."""


@dataclass(frozen=True)
class CellsBatch:
    """One recompute pass's patches for a grid watch (possibly empty —
    the settle marker).

    Attributes:
        patches: The changed pieces, in fold order.
    """

    patches: tuple[CellsPatch, ...]
    kind: Literal["batch"] = "batch"


@dataclass(frozen=True)
class CellsRecomputing:
    """A slow-flagged grid recompute began; the terminal batch settles
    it."""

    kind: Literal["recomputing"] = "recomputing"


@dataclass(frozen=True)
class CellsGone:
    """The watched grid no longer exists (tombstone). The watch stays
    live; re-creation at the same path resumes with a fresh snapshot
    batch (with fresh grants in its :class:`GridMeta`)."""

    kind: Literal["subject_gone"] = "subject_gone"


type CellsWatchEvent = CellsBatch | CellsRecomputing | CellsGone
"""One ``watch_cells`` delivery."""


# -- the cell watch (watch_cell) ---------------------------------------------------


@dataclass(frozen=True)
class CellReportUpdate:
    """Replace the cell's inspection snapshot (content with verdicts —
    the ``inspect_cell`` shape; empty cells arrive as the all-``None``
    report, never a fault).

    Attributes:
        report: The fresh :class:`~fxtr_experiment.user_api.CellReport`.
    """

    report: CellReport
    kind: Literal["report"] = "report"


@dataclass(frozen=True)
class CellGenerationUpdate:
    """Replace the task cell's generation report (task cells only).

    Attributes:
        report: The fresh
            :class:`~fxtr_experiment.user_api.GenerationReport` (its
            never-fault-on-emptiness convention included: a cell with no
            current generation reports ``generation is None``).
    """

    report: GenerationReport
    kind: Literal["generation"] = "generation"


@dataclass(frozen=True)
class CellObserversUpdate:
    """Replace the cell's reverse-observer list (task cells only).

    Attributes:
        observers: The fresh ``list_observers`` shape.
    """

    observers: tuple[ObserverInfo, ...]
    kind: Literal["observers"] = "observers"


type CellPatch = CellReportUpdate | CellGenerationUpdate | CellObserversUpdate
"""One cell-mirror patch (all whole-value replaces — the drawer bundle's
pieces are small)."""


@dataclass(frozen=True)
class CellBatch:
    """One recompute pass's patches for a cell watch (possibly empty —
    the settle marker).

    Attributes:
        patches: The changed pieces, in fold order.
    """

    patches: tuple[CellPatch, ...]
    kind: Literal["batch"] = "batch"


@dataclass(frozen=True)
class CellRecomputing:
    """A slow-flagged cell recompute began; the terminal batch settles
    it."""

    kind: Literal["recomputing"] = "recomputing"


@dataclass(frozen=True)
class CellGone:
    """The watched cell's GRID no longer exists (tombstone; cell
    emptiness is data — an empty report — never a tombstone). The watch
    stays live; grid re-creation resumes with a fresh snapshot batch."""

    kind: Literal["subject_gone"] = "subject_gone"


@dataclass(frozen=True)
class CellLogReset:
    """A new run restarted the cell's run-log buffer: drop accumulated
    entries (appends for the new run follow).

    Attributes:
        generation: The new run's public generation id.
    """

    generation: str
    kind: Literal["log_reset"] = "log_reset"


@dataclass(frozen=True)
class CellLogAppend:
    """Appended run-log entries (event-native: published straight from
    the run-log buffer, not diffed). The entries already buffered at
    subscribe time arrive as one of these right after the first batch.

    Attributes:
        entries: The appended entries, in emission order.
    """

    entries: tuple[LogEntry, ...]
    kind: Literal["log_append"] = "log_append"


type CellWatchEvent = CellBatch | CellRecomputing | CellGone | CellLogReset | CellLogAppend
"""One ``watch_cell`` delivery."""


# -- the grid-graph watch (watch_graph) -------------------------------------------


@dataclass(frozen=True)
class GraphUpdate:
    """Replace the grid-graph overlay (whole-value — it changes rarely, so
    the overlay ships whole rather than as keyed patches; the engine's
    diff-vs-last-sent suppresses no-ops, and splitting the *recompute* into
    structure/edges halves is what avoids re-decoding rules on turnover —
    see `notes/plate_view.md`).

    Attributes:
        graph: The fresh :class:`~fxtr_experiment.user_api.GridGraph`.
    """

    graph: GridGraph
    kind: Literal["graph"] = "graph"


@dataclass(frozen=True)
class GraphBatch:
    """One recompute pass's patches for the graph watch (possibly empty —
    the settle marker).

    Attributes:
        patches: The changed pieces, in fold order (at most one
            :class:`GraphUpdate`).
    """

    patches: tuple[GraphUpdate, ...]
    kind: Literal["batch"] = "batch"


@dataclass(frozen=True)
class GraphRecomputing:
    """A slow-flagged grid-graph recompute began (the aggregation joins +
    rule decode); the terminal batch settles it."""

    kind: Literal["recomputing"] = "recomputing"


type GraphWatchEvent = GraphBatch | GraphRecomputing
"""One ``watch_graph`` delivery."""
