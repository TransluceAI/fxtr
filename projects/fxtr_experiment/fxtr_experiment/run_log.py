"""The run-log buffer: runtime-only per-cell log streams.

The event-native half of the cell watch (``notes/event_system.md``): per
task cell, the current run's log entries — task-authored step ``log()``
calls plus the engine's lifecycle notes (wrote/read cell, demand, step
started / record-hit) — buffered in memory on the runner and streamed to
live cell watches. Nothing here is durable or persisted: buffers restart
with each run, survive the run's end (an errored run's entries are
exactly what the drawer wants), and vanish with the runner. The state
db's passive ``events`` table is unrelated (an op-log for durable
provenance, not a message stream).

Ownership and wiring: the :class:`RunLogRegistry` lives on the
`~fxtr_experiment.executor.Executor` (which owns the run lifecycle —
``_run_once`` starts each run's buffer); the per-run
:class:`RunLogSink` is handed to the `~fxtr_experiment.ops_engine.
OpsEngine` at load (the live run gets a real sink, the safety pass's
frozen engine gets ``None`` — replay silence is structural); the watch
engine registers the append listener and serves snapshots to fresh
subscriptions.

Concurrency: everything here is sync and runs on the runner's one event
loop (engine ops call :meth:`RunLogSink.note` between awaits; the
listener must not block — the
`~fxtr_experiment.state_store.WorkflowSession.set_event_listener`
convention). Consequence of the restart-per-run contract: a *resume*
clears the interrupted run's entries and record hits don't re-emit
``log()`` lines, so a resumed buffer holds post-frontier entries only
(hit-path lifecycle notes show the replay's progress instead).
"""

from __future__ import annotations

import datetime
from typing import Callable

from fxtr_experiment.watch_api import (
    CellLogAppend,
    CellLogReset,
    LogEntry,
    LogEntryKind,
    StepRef,
)

Cell = tuple[str, str]
"""One cell's identity here: ``(grid key-path text, flattened-coords
text)`` — the watch addressing."""

RunLogListener = Callable[[str, str, "CellLogReset | CellLogAppend"], None]
"""The append listener: ``(grid_path, coords, event)``, called sync at
note/restart time. Must not block (see the module docstring)."""

MAX_ENTRIES_PER_CELL = 2000
"""Per-cell buffer bound (drop-oldest): a runaway logging loop must not
balloon runner memory. Live watches saw the dropped entries as appends;
only late snapshots are truncated."""


def _now_iso() -> str:
    """Wall-clock UTC timestamp in ISO form (the failure-row convention)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class _CellBuffer:
    """One cell's current-run entries (private to the registry)."""

    __slots__ = ("generation", "entries")

    def __init__(self, generation: str) -> None:
        self.generation = generation
        self.entries: list[LogEntry] = []


class RunLogSink:
    """One run's write handle, bound to its cell and generation.

    Handed to the ops engine at load; engine code calls :meth:`note`
    unconditionally-cheaply (a ``None`` sink engine-side means discard —
    the safety pass). A sink outliving its run is harmless but inert-by-
    convention: the executor mints a fresh sink per run, and a stale
    sink's notes would land in the next run's buffer, so holders must
    not note after their run ends (the engine's lifetime enforces this
    naturally — it is dropped with the run scope).
    """

    __slots__ = ("_registry", "_cell")

    def __init__(self, registry: "RunLogRegistry", cell: Cell) -> None:
        self._registry = registry
        self._cell = cell

    def note(self, entry_kind: LogEntryKind, message: str, *, step: StepRef | None = None) -> None:
        """Append one entry (sync; timestamps here).

        Args:
            entry_kind: What produced the entry (`watch_api.LogEntryKind`).
            message: The entry text.
            step: The step this entry belongs to (`watch_api.StepRef`),
                for entries produced during a step invocation.
        """
        entry = LogEntry(ts=_now_iso(), entry_kind=entry_kind, step=step, message=message)
        self._registry._append(self._cell, entry)


class RunLogRegistry:
    """The runner's per-cell run-log buffers (executor-owned).

    One buffer per task cell, holding the CURRENT run's entries (see the
    module docstring for the lifecycle). The watch engine consumes via
    :meth:`snapshot` (a fresh subscription's backlog) and
    :meth:`set_listener` (live resets/appends).
    """

    def __init__(self) -> None:
        self._buffers: dict[Cell, _CellBuffer] = {}
        self._listener: RunLogListener | None = None

    def set_listener(self, listener: RunLogListener | None) -> None:
        """Install (or clear) THE append listener — one consumer, the
        capability mount's watch engine; fan-out belongs behind it."""
        self._listener = listener

    def start_run(self, grid_path: str, coords: str, generation: str) -> RunLogSink:
        """Restart the cell's buffer for a new run and mint its sink.

        Clears any previous run's entries (the restart-per-run contract)
        and notifies the listener with a `watch_api.CellLogReset` so live
        watches drop their accumulated entries.
        """
        cell = (grid_path, coords)
        self._buffers[cell] = _CellBuffer(generation)
        if self._listener is not None:
            self._listener(grid_path, coords, CellLogReset(generation=generation))
        return RunLogSink(self, cell)

    def snapshot(self, grid_path: str, coords: str) -> tuple[str, tuple[LogEntry, ...]] | None:
        """The cell's buffered ``(generation, entries)``, or ``None`` if
        no run has started since the runner came up."""
        buffer = self._buffers.get((grid_path, coords))
        if buffer is None:
            return None
        return buffer.generation, tuple(buffer.entries)

    def drop_cell(self, grid_path: str, coords: str) -> None:
        """Forget a cell's buffer (its grid was deleted / its state
        cleared — no run to show)."""
        self._buffers.pop((grid_path, coords), None)

    def _append(self, cell: Cell, entry: LogEntry) -> None:
        """Sink-side append: buffer (bounded) + notify."""
        buffer = self._buffers.get(cell)
        if buffer is None:
            # A sink noting after drop_cell (e.g. an op racing a grid
            # delete): nothing to attach the entry to; drop it.
            return
        buffer.entries.append(entry)
        if len(buffer.entries) > MAX_ENTRIES_PER_CELL:
            del buffer.entries[:-MAX_ENTRIES_PER_CELL]
        if self._listener is not None:
            self._listener(cell[0], cell[1], CellLogAppend(entries=(entry,)))
