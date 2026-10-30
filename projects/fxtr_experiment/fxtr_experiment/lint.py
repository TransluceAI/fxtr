"""Lint enumeration: the whole-workflow structural inventory, store-internal
to :meth:`~fxtr_experiment.state_store.WorkflowSession.lint`.

The linter is the intent axis's whole-workflow query: where `grounding.py`
judges one fact, the lint sweep enumerates every fact and every intent
record at one session snapshot and reports the structural problems —
dangling intent (sourcing records / targets whose pinned fingerprint no
longer matches the world), attached targets whose materialized fact is not
grounded, and the inventory of every non-grounded fact. Staleness is
deliberately not consulted: lint findings mean "this experiment is not
constructed properly" — problems one fixes by editing intent or structure
— while freshness is currency's operational question ("rerun needed"),
judged per cell elsewhere. The linter thus checks the *structural half* of
the replay contract's hypothesis (see the linter section of
``notes/grounding.md``, which also carries the two-level clean state:
*sound* — no dangling intent, no red targets — and *clean* — sound plus an
empty inventory).

Two deliberate non-findings:

- **An unmaterialized attached target is pending intent**, not a problem:
  a target on a cell with no current generation (or an unpopulated data
  cell) has no fact to grade, and its remedy — demand it — is
  operational, not structural.
- **Inventory entries are not always prunable.** The inventory is simply
  every non-grounded fact. Once the report is sound, cone-transitivity of
  grounding makes the entries exactly the facts outside every demanded
  cone (the prune candidates); while a target is red, the same section
  also contains the *causes* of the redness (named by the red target's
  diagnosis chain). No cone membership is computed to separate the two.

This module owns the sync enumeration bodies (each runs inside the
caller's transaction) and the row/result types; the per-fact verdicts come
from the grounding evaluator, composed under one hold of the session lock
in `state_store.py` — which is what makes the report a single-snapshot
fact.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment.grounding import Cell, GroundingVerdict
from fxtr_experiment.storage import id_of

AttachmentStatus = Literal["attached", "dangling_missing", "dangling_mismatched"]
"""Attachment state of an intent record (the shared target/sourcing
vocabulary — `user_api.py` aliases the same literals)."""

FactKind = Literal["grid", "data_cell", "task_cell"]
"""The graded fact kinds of ``notes/grounding.md``: a grid with its
config, a populated data/handle cell, a task cell's current generation."""


@dataclass(frozen=True)
class LintSourcingRow:
    """One dangling user sourcing record (grid record iff ``coords == ''``).

    Attributes:
        grid_path: The subject grid's key-path text.
        coords: The subject cell's flattened-coords text; ``''`` for the
            grid-level record.
        fingerprint: The pinned config/value id (NOT re-attached — the
            session return is in-process; the wire layer re-attaches).
        status: Why it dangles (never ``"attached"`` — attached records
            are not findings).
    """

    grid_path: str
    coords: str
    fingerprint: FxtrID
    status: AttachmentStatus


@dataclass(frozen=True)
class LintTargetRow:
    """One problem target: dangling, or attached with an ungrounded
    materialized fact.

    Attributes:
        slice_id: Entity id of the target's stored slice template (NOT
            re-attached; see :class:`LintSourcingRow`).
        expected_config: The target's pinned config id (ditto).
        grid_path: The wanted grid's key-path text (denormalized).
        coords: The wanted cell's flattened-coords text.
        status: Attachment state; ``"attached"`` rows are here because
            their fact is ungrounded.
        verdict: The materialized fact's verdict, for attached rows
            (task cells: the current generation's; data cells: the
            value's); ``None`` for dangling rows (nothing gradable).
        reason: The verdict's diagnosis (``None`` iff ``verdict`` is).
    """

    slice_id: FxtrID
    expected_config: FxtrID
    grid_path: str
    coords: str
    status: AttachmentStatus
    verdict: GroundingVerdict | None
    reason: str | None


@dataclass(frozen=True)
class LintUngroundedRow:
    """One non-grounded fact in the inventory.

    Attributes:
        kind: Which fact kind (see :data:`FactKind`).
        grid_path: The fact's grid key-path text.
        coords: The cell's flattened-coords text; ``''`` for grids.
        verdict: ``"orphaned"`` (no claimant at the current config/value)
            or ``"unanchored"`` (claimed, but the chain does not derive)
            — never ``"grounded"``.
        reason: The verdict's diagnosis.
    """

    kind: FactKind
    grid_path: str
    coords: str
    verdict: GroundingVerdict
    reason: str | None


@dataclass(frozen=True)
class LintState:
    """The session-level lint snapshot (findings only; primitives and raw
    ids). The user surface maps this to `user_api.LintReport`, loading
    slice templates and re-attaching served ids there."""

    dangling_sourcing: tuple[LintSourcingRow, ...]
    targets: tuple[LintTargetRow, ...]
    ungrounded: tuple[LintUngroundedRow, ...]


@dataclass(frozen=True)
class TargetScan:
    """One target row as enumerated (pre-verdict; internal to the sweep).

    Attributes:
        slice_id: Entity id of the stored slice template.
        expected_config: The pinned config id.
        grid_path: Denormalized wanted-grid path.
        coords: Denormalized wanted-cell coords.
        status: Attachment state against the live grid row.
        materialized: For attached rows: whether the graded fact exists
            (task grids: a current generation; other kinds: a populated
            cell). Always ``False`` for dangling rows.
    """

    slice_id: FxtrID
    expected_config: FxtrID
    grid_path: str
    coords: str
    status: AttachmentStatus
    materialized: bool


def collect_facts(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, ...], tuple[Cell, ...], tuple[Cell, ...]]:
    """Enumerate every graded fact at the current snapshot.

    Returns:
        ``(grid paths, data/handle cells, task cells with a current
        generation)``, each in deterministic (path, coords) order. Task
        cells come from ``task_execs`` (a cell with no generation is no
        fact); checkpoint entries are not graded facts and have no rows
        in these tables.
    """
    grids = tuple(
        r[0] for r in conn.execute("SELECT key_path FROM grids ORDER BY key_path").fetchall()
    )
    dcells = tuple(
        (r[0], r[1])
        for r in conn.execute(
            "SELECT grid_path, coords FROM cells ORDER BY grid_path, coords"
        ).fetchall()
    )
    tcells = tuple(
        (r[0], r[1])
        for r in conn.execute(
            "SELECT grid_path, coords FROM task_execs ORDER BY grid_path, coords"
        ).fetchall()
    )
    return grids, dcells, tcells


def collect_targets(conn: sqlite3.Connection) -> tuple[TargetScan, ...]:
    """Enumerate every target with its attachment status and (for attached
    rows) whether its fact is materialized. Runs in the caller's
    transaction; the denormalized location columns serve the lookups
    (targets are fully bound)."""
    rows = []
    for slice_blob, expected_blob, grid_path, coords in conn.execute(
        "SELECT slice_id, expected_config, grid_path, coords FROM targets"
        " ORDER BY grid_path, coords"
    ).fetchall():
        grid = conn.execute(
            "SELECT kind, config_id FROM grids WHERE key_path = ?", (grid_path,)
        ).fetchone()
        materialized = False
        if grid is None:
            status: AttachmentStatus = "dangling_missing"
        elif grid[1] != expected_blob:
            status = "dangling_mismatched"
        else:
            status = "attached"
            fact_table = "task_execs" if grid[0] == "task" else "cells"
            materialized = (
                conn.execute(
                    f"SELECT 1 FROM {fact_table} WHERE grid_path = ? AND coords = ?",
                    (grid_path, coords),
                ).fetchone()
                is not None
            )
        rows.append(
            TargetScan(
                slice_id=id_of(slice_blob),
                expected_config=id_of(expected_blob),
                grid_path=grid_path,
                coords=coords,
                status=status,
                materialized=materialized,
            )
        )
    return tuple(rows)


def dangling_sourcing_rows(conn: sqlite3.Connection) -> tuple[LintSourcingRow, ...]:
    """Enumerate the dangling user sourcing records (attached records are
    not findings): the pinned fingerprint is compared against the live
    grid config (grid records) or cell value (cell records)."""
    rows = []
    for grid_path, coords, fingerprint in conn.execute(
        "SELECT grid_path, coords, fingerprint FROM user_sourcing ORDER BY grid_path, coords"
    ).fetchall():
        if coords == "":
            live = conn.execute(
                "SELECT config_id FROM grids WHERE key_path = ?", (grid_path,)
            ).fetchone()
        else:
            live = conn.execute(
                "SELECT value_id FROM cells WHERE grid_path = ? AND coords = ?",
                (grid_path, coords),
            ).fetchone()
        if live is not None and live[0] == fingerprint:
            continue  # attached
        rows.append(
            LintSourcingRow(
                grid_path=grid_path,
                coords=coords,
                fingerprint=id_of(fingerprint),
                status="dangling_missing" if live is None else "dangling_mismatched",
            )
        )
    return tuple(rows)
