"""Currency evaluation: fresh / stale / unsettled, cached in the state db.

Implements the currency model of ``notes/execution_semantics.md`` for one
task cell's current generation, by comparing its recorded observations
against the live grid contents:

- **stale** — some observation's subject now holds a *settled* value
  different from the recorded fingerprint: a user-edited or deleted data
  cell, a changed/deleted grid config, or an upstream generation that
  completed with a different output. A definite, local fact.
- **fresh** — every observation matches its subject's settled value and
  every observed task output's cell is itself fresh, with **well-founded
  support**: the justification bottoms out in data cells and settled task
  results, so observation cycles can never prove freshness.
- **unsettled** — neither: nothing definitely changed, but the support
  DAG contains something without a settled result (cleared, running,
  errored, interrupted) or something stale/unsettled/cyclic. Resolves to
  fresh or stale when the support settles.

**Verdicts are value-pinned snapshot facts.** An evaluation runs as one
sync body under the session's writer lock (this module is the
implementation of :meth:`~fxtr_experiment.state_store.WorkflowSession.
cell_currency`; nothing else calls it), so the whole walk sees a single
consistent snapshot — and the report of a complete generation carries the
``results`` read from that same snapshot. The claim is therefore
"generation G with results R is fresh", never the racy pair "some value
is current" + a separately-fetched value: consumers (the executor's
demand, and through it the engine) serve and record exactly the validated
results, closing the check-then-read window.

**The stamps are the memo.** A settled (terminal-status) generation's
derived verdict is written back onto its ``task_execs`` row — the
*currency stamp* (``currency_verdict`` / ``currency_reason`` /
``currency_as_of``, NULL together) — and mutations *clear* stamps up the
observer DAG (`state_store.py`'s marking helpers; invariants + trigger
table: ``notes/execution_semantics.md``). Serving is O(1) while the stamp
holds: a settled, stamped generation's verdict is current by the marking
invariant, no walk needed. Running generations are never stamped (their
record is still growing; they always derive live), and all verdicts stamp
— a stale stamp is invalidated by the revert-write's mark (resurrection
is the ordinary cycle), an unsettled-because-running stamp by the status
flip, and an unsettled-because-cycle stamp correctly persists until some
member changes.

**Evaluation shape.** One recursive CTE bulk-loads the *dirty support
cone* — the generations reachable from the queried cell through
observation edges, expanding only through unstamped-or-running nodes —
together with their records and the subjects' current values; the verdict
logic (well-founded freshness, the in-progress cycle marker parking at
unsettled, per-edge fingerprint comparison, diagnosis construction) then
runs in Python over the loaded cone, and the derived verdicts of settled
nodes are stamped back, all inside the one transaction. The evaluation is
memoized *within* the walk; the memo's in-progress marker is unsettled,
so cycles need no special case — they simply fail to acquire well-founded
support. A definite mismatch in a cell's *own* record wins over unsettled
support (stale is settled evidence; unsettled is absence of evidence),
and support verdicts never make an observer stale — only unsettled.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Sequence, cast

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment_task.model import Currency
from fxtr_experiment.storage import OBS_KIND_GRID_CONFIG, id_of

_Cell = tuple[str, str]  # (grid_path, coords)
_Memo = dict[_Cell, "CurrencyReport | None"]


@dataclass(frozen=True)
class CurrencyReport:
    """One cell's currency verdict, pinned to the generation and values it
    was evaluated against (all read at a single snapshot).

    Attributes:
        currency: The tri-state verdict.
        reason: ``None`` iff fresh; otherwise the first cause found (the
            demand policy's fault diagnosis — e.g. which subject changed,
            or which support cell is unsettled and why).
        generation: The evaluated generation's public id.
        status: Its execution status at the snapshot.
        results: Its per-output results at the snapshot, for ``complete``
            generations (``None`` otherwise). When ``currency`` is fresh
            and ``status`` complete, this is *the* validated value —
            consumers must serve/record from it rather than re-reading.
    """

    currency: Currency
    reason: str | None
    generation: str
    status: str
    results: Mapping[str, FxtrID] | None


@dataclass
class _Node:
    """One cone member: a generation row + everything its evaluation needs.

    ``record`` rows are ``(sub_path, sub_coords, output, fingerprint,
    kind, current_cell_value, current_grid_config)`` — the observation
    joined with the subject's *current* contents at the same snapshot
    (``None`` where the join has no row); only populated for nodes that
    will be derived (unstamped or running).
    """

    exec_id: int
    generation: str
    status: str
    verdict: str | None
    reason: str | None
    results: dict[str, FxtrID] = field(default_factory=dict)
    record: list[tuple[str, str, str, FxtrID, int, FxtrID | None, FxtrID | None]] = field(
        default_factory=list
    )

    @property
    def stamped(self) -> bool:
        """Serve-fast condition: a settled generation with a live stamp."""
        return self.verdict is not None and self.status != "running"


def evaluate_cell(conn: sqlite3.Connection, grid_path: str, coords: str) -> CurrencyReport | None:
    """Evaluate one task cell's currency: serve the stamp or derive + stamp.

    The :meth:`WorkflowSession.cell_currency` body — one transaction
    covering the whole read *and* the stamp write-back, so the report is
    a single-snapshot fact and stamps are atomic with the walk that
    derived them.

    Args:
        grid_path: The task grid's key-path text.
        coords: The cell's flattened-coords text.

    Returns:
        The snapshot report for the cell's current generation, or ``None``
        if the cell has no current generation (nothing to be current
        *about*; the demand policy's no-generation row applies instead).
    """
    with conn:
        cone = _load_cone(conn, grid_path, coords)
        root = (grid_path, coords)
        if root not in cone:
            return None
        memo: _Memo = {}
        derived: set[_Cell] = set()
        report = _evaluate(cone, root, memo, derived)
        _write_stamps(conn, cone, memo, derived)
        return report


def _evaluate(
    cone: dict[_Cell, _Node], cell: _Cell, memo: _Memo, derived: set[_Cell]
) -> CurrencyReport | None:
    """The memoized recursion (pure Python over the loaded cone).

    Serves stamped settled nodes in O(1); derives the rest exactly as the
    currency model states, recording them in ``derived`` for the caller's
    stamp write-back.
    """
    if cell in memo:
        return memo[cell]
    node = cone.get(cell)
    if node is None:
        memo[cell] = None
        return None
    results = node.results if node.status == "complete" else None
    if node.stamped:
        assert node.verdict in ("fresh", "stale", "unsettled")  # column contract
        verdict = cast(Currency, node.verdict)
        report = CurrencyReport(verdict, node.reason, node.generation, node.status, results)
        memo[cell] = report
        return report
    derived.add(cell)
    grid_path, coords = cell
    # In-progress marker: a cell participating in its own support cannot
    # prove freshness (well-foundedness); if a real mismatch exists
    # elsewhere in the cycle, that cell's own evaluation still finds it.
    memo[cell] = CurrencyReport(
        "unsettled",
        f"{grid_path}{coords} is in its own support (observation cycle)",
        node.generation,
        node.status,
        results,
    )
    stale_reason: str | None = None
    unsettled_reason: str | None = None
    for sub_path, sub_coords, output, fingerprint, kind, cur_cell, cur_config in node.record:
        if kind == OBS_KIND_GRID_CONFIG:
            if cur_config is None:
                stale_reason = f"observed grid {sub_path} was deleted"
                break
            if cur_config != fingerprint:
                stale_reason = f"observed grid {sub_path} has a different config"
                break
        elif output:  # a task-output observation: settledness + support
            sub = cone.get((sub_path, sub_coords))
            if sub is None or sub.status != "complete":
                sub_status = "cleared" if sub is None else sub.status
                unsettled_reason = unsettled_reason or (
                    f"support {sub_path}{sub_coords} has no settled result ({sub_status})"
                )
                continue
            if sub.results.get(output) != fingerprint:
                stale_reason = (
                    f"support {sub_path}{sub_coords} settled output {output!r}"
                    " on a different value"
                )
                break
            sub_report = _evaluate(cone, (sub_path, sub_coords), memo, derived)
            assert sub_report is not None  # sub is in the cone (checked above)
            if sub_report.currency != "fresh":
                unsettled_reason = unsettled_reason or (
                    f"support {sub_path}{sub_coords} is {sub_report.currency}"
                    f" ({sub_report.reason})"
                )
        else:  # cell_value / wrote_cell on a data/handle cell (settled ground)
            if cur_cell != fingerprint:
                what = "was deleted" if cur_cell is None else "holds a different value"
                stale_reason = f"observed cell {sub_path}{sub_coords} {what}"
                break
    if stale_reason is not None:
        report = CurrencyReport("stale", stale_reason, node.generation, node.status, results)
    elif unsettled_reason is not None:
        report = CurrencyReport(
            "unsettled", unsettled_reason, node.generation, node.status, results
        )
    else:
        report = CurrencyReport("fresh", None, node.generation, node.status, results)
    memo[cell] = report
    return report


# -- cone loading (the SQL-native traversal) --------------------------------------


def _load_cone(conn: sqlite3.Connection, grid_path: str, coords: str) -> dict[_Cell, _Node]:
    """Bulk-load the queried cell's support cone at one snapshot.

    One recursive CTE collects the generations reachable from the cell
    through task-output observation edges, expanding only through
    unstamped-or-running nodes (stamped settled support serves its stamp,
    so its own support is never needed); records + current subject values
    are then bulk-fetched for exactly the nodes that will be derived.
    """
    rows = conn.execute(
        """
        WITH RECURSIVE cone(exec_id) AS (
            SELECT te.exec_id FROM task_execs te
              WHERE te.grid_path = ? AND te.coords = ?
            UNION
            SELECT te2.exec_id
              FROM cone c
              JOIN task_execs te ON te.exec_id = c.exec_id
              JOIN observations o ON o.exec_id = c.exec_id
              JOIN subject_versions sv ON sv.version_id = o.version_id
              JOIN task_execs te2
                ON te2.grid_path = sv.grid_path AND te2.coords = sv.coords
              WHERE sv.output != ''
                AND (te.currency_verdict IS NULL OR te.status = 'running')
        )
        SELECT te.exec_id, te.grid_path, te.coords, te.generation, te.status,
               te.currency_verdict, te.currency_reason
          FROM cone JOIN task_execs te ON te.exec_id = cone.exec_id
        """,
        (grid_path, coords),
    ).fetchall()
    cone: dict[_Cell, _Node] = {}
    by_exec: dict[int, _Node] = {}
    for exec_id, g, c, generation, status, verdict, reason in rows:
        node = _Node(exec_id, generation, status, verdict, reason)
        cone[(g, c)] = node
        by_exec[exec_id] = node
    for batch in _chunks(by_exec):
        marks = ",".join("?" * len(batch))
        for exec_id, output, blob in conn.execute(
            f"SELECT exec_id, output, value_id FROM task_results" f" WHERE exec_id IN ({marks})",
            batch,
        ):
            by_exec[exec_id].results[output] = id_of(blob)
    derive_ids = [n.exec_id for n in by_exec.values() if not n.stamped]
    for batch in _chunks(derive_ids):
        marks = ",".join("?" * len(batch))
        for exec_id, g, c, out, fp, kind, cur_cell, cur_cfg in conn.execute(
            f"""
            SELECT o.exec_id, sv.grid_path, sv.coords, sv.output, sv.fingerprint,
                   o.kind, cells.value_id, grids.config_id
              FROM observations o
              JOIN subject_versions sv ON sv.version_id = o.version_id
              LEFT JOIN cells ON cells.grid_path = sv.grid_path
                             AND cells.coords = sv.coords
              LEFT JOIN grids ON grids.key_path = sv.grid_path
              WHERE o.exec_id IN ({marks})
            """,
            batch,
        ):
            by_exec[exec_id].record.append(
                (
                    g,
                    c,
                    out,
                    id_of(fp),
                    kind,
                    None if cur_cell is None else id_of(cur_cell),
                    None if cur_cfg is None else id_of(cur_cfg),
                )
            )
    return cone


def _write_stamps(
    conn: sqlite3.Connection, cone: dict[_Cell, _Node], memo: _Memo, derived: set[_Cell]
) -> None:
    """Stamp every *derived, settled* node's verdict back onto its row.

    Running generations never stamp (their record is still growing —
    stamping would cache a moving target); served-from-stamp nodes are
    untouched. Same transaction as the walk, so a stamp is never newer
    than the snapshot it summarizes.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stamps = []
    for cell in derived:
        node = cone[cell]
        if node.status == "running":
            continue
        report = memo[cell]
        assert report is not None  # derived cells always resolve to a report
        stamps.append((report.currency, report.reason, now, node.exec_id))
    conn.executemany(
        "UPDATE task_execs SET currency_verdict = ?, currency_reason = ?,"
        " currency_as_of = ? WHERE exec_id = ?",
        stamps,
    )


def _chunks(ids: Iterable[int], size: int = 500) -> Iterator[Sequence[int]]:
    """Chunk an id collection for bounded ``IN (...)`` parameter lists."""
    batch = list(ids)
    for i in range(0, len(batch), size):
        yield batch[i : i + size]
