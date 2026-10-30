"""Grounding evaluation: the wanted / would_start / grounded_* least fixed
point of ``notes/grounding.md``, cached in the state db.

This module is the store-internal implementation of
:meth:`~fxtr_experiment.state_store.WorkflowSession.cell_grounding` /
:meth:`~fxtr_experiment.state_store.WorkflowSession.grid_grounding` (the
evaluator) and of every mutating body's grounding marks (the
:func:`mark_grounding` propagation). It is the sibling of `currency.py`,
one axis over: currency judges *values* against recorded observations;
grounding judges *intent and structure* — would a from-scratch replay
re-create this fact — and is deliberately fingerprint-blind about
everything except which claims match the *current* config/value.

The rest of this docstring is the correctness argument, written for a
reader who knows ``notes/grounding.md`` but not this code: (a) the
invariants that always hold of the DB + in-memory state, (b) what they
imply about the state ↔ datalog-predicate relationship, (c) why each
mutating function upholds them, (d) why each derived-property query
computes the datalog's answer.

(a) Invariants
==============

**I-G1 (fact representation).** The datalog's extensional relations are
exactly these queries over the state db, at any single snapshot:

====================  ============================================================
datalog fact          state-db representation
====================  ============================================================
grid_current(g, C)    ``grids`` row (key_path, config_id)
data_current(d, v)    ``cells`` row (grid_path, coords, value_id)
gen_current(c, e)     ``task_execs`` row (grid_path, coords) — status-blind
                      (see "Execution status and the predicates" in the note)
grid_dep(g, g')       ``grid_refs`` rows (terminal rule refs) ∪ the parent edge
                      (``grids.parent_path``; the zero-axis root is not a row
                      and is vacuously grounded)
user_sources_grid     ``user_sourcing`` row with ``coords = ''``
user_sources_cell     ``user_sourcing`` row with ``coords != ''``
target_task(c, C)     ``targets`` row (via the denormalized grid_path/coords);
                      *attached* iff C == the grid's current config_id
asserted_grid(e,g,C)  ``grid_assertions`` ⋈ ``subject_versions`` at (g,'','')
wrote(e, d, v)        ``observations`` kind=wrote_cell ⋈ versions at (d, '')
read_out(e, c)        ``request_edges`` row, kind=task_output — recorded at
                      request *commitment* (record-miss time), resolution-blind,
                      cell-granular (wantedness never needs the output name)
read_data(e, d)       ``request_edges`` row, kind=data (data/handle reads)
mint_compatible(g,g') derived live from config *entities* (immutable,
                      content-addressed — see (d))
====================  ============================================================

The demand facts read the *request* record, never the observation
record: a request edge is durable the moment a running generation
commits to a request, before anything resolves — which is what lets a
fresh cascade's ``wanted`` derive from durable state at every moment
(``notes/grounding.md``, self-vouching). The two axes split cleanly:
currency reads the resolved-value record (observations), grounding
reads the request record. For complete generations the two coincide —
*enforced* by the safety pass's request-edge comparison
(``notes/execution_semantics.md``), never assumed.

**I-G2 (stamp soundness).** Whenever a grounding stamp is non-NULL (and
whenever a ``wanted_stamps`` row exists), its verdict equals what the
datalog LFP derives from the current extensional state. Contrapositive —
the operational form: *every mutation that could change some fact's
derived verdict clears that fact's stamp in the same transaction* (see
(c)).

**I-G3 (snapshot stamping).** Evaluation writes only verdicts it derived
at the current snapshot, in the same locked session op as the derivation,
and never stamps a running generation's own verdict (its record is a
moving target; it always derives live).

**I-G4 (the prune invariant, P).** At all times: *every stamped fact's
justification cone contains only stamped facts and running generations.*
("Justification cone" = the facts its datalog clauses consult,
transitively; running generations are the one sanctioned unstamped cone
member, because their edge growth marks dependents directly — see (c).)

(b) What the invariants imply
=============================

By I-G1, evaluating the datalog at a snapshot is a pure function of the
tables above plus config entities. By I-G2, a live stamp *is* that
function's value — serving it is O(1) and needs no walk. By I-G3+I-G4,
stamps written by one evaluation remain sound until a mutation clears
them: any extensional change reaches every stamped dependent through the
marking propagation (P is exactly what makes the propagation's
prune-at-unstamped sound: if a stamped fact depended on an unstamped one,
P would already be violated — so once the walk reaches an unstamped fact
it can stop; nothing stamped lies beyond it).

P is preserved inductively:

- *Evaluation* stamps the entire cone it derived (modulo running
  generations, which P explicitly allows), so new stamps enter with
  all-stamped cones.
- *Mutations* seed :func:`mark_grounding` with every fact whose immediate
  justification inputs they changed (the trigger table in (c)); the
  propagation clears all stamped transitive dependents. The would_start
  fan (below) is the one subtlety: ``would_start(e)`` is never itself
  stamped — its value is baked into its dependents — so whenever one of
  its *inputs* (``wanted(cell(e))`` or ``grounded_grid(grid(e))``) is
  cleared, the walk fans to would_start's dependents (e's read_out
  subjects' wanted stamps, e's written cells, e's asserted grids, e's own
  generation stamp) without consulting e's stamp state; and when e's
  *record grows* (a new edge), the insert seeds the affected dependent
  directly. Both paths together keep P for facts citing running or
  unstamped generations.

(c) Mutation obligations (the trigger table)
============================================

Each mutating body in `state_store.py` declares its grounding marks in
its docstring ("Grounding marks:"); this is the master table, with the
justification for each entry. Seeds name the facts whose immediate
inputs changed; :func:`mark_grounding` handles the transitive part.

- **request-edge insert, task-output kind** → seed WANTED(subject cell):
  the edge is a new ``read_out`` fact feeding ``wanted``. (A data-kind
  insert seeds nothing: it feeds only the *requester's own*
  ``grounded_task_cell``, and the requester is running, hence unstamped
  by I-G3. Observation inserts seed nothing wanted-side — resolution
  adds no demand fact the commitment didn't already add.)
- **wrote edge insert / cell write** → seed DCELL(subject): the cell's
  claim set (or value) changed.
- **grid assertion insert** → seed GRID(g): the grid's claim set changed.
- **sourcing upsert / revoke** → seed GRID(g) or DCELL(d) per row kind
  (cascade revoke seeds every deleted row's fact, plus WANTED for every
  deleted target).
- **target add / remove** → seed WANTED(c).
- **grid create** → seed the referrer grids (their ``grid_dep`` input
  flipped from missing to present) and WANTED of cells targeted at the
  path (attachment may flip). The new grid's own rows are born unstamped.
- **grid delete** → seed the referrer grids; the reader generations of
  every subject under the grid (their input facts vanished); and the
  departing-record fan of every generation *inside* the grid (as for
  turnover, below — enumerated before the cascade). ``wanted_stamps``
  rows under the grid are deleted (their locations are gone).
- **generation turnover / clear** → the departing record's fan: WANTED of
  its task-output request subjects, DCELL of its written cells, GRID of
  its asserted grids (the claims/demand edges vanish), plus GEN of the
  requesters of this cell's outputs (their ``grounded_task_cell``
  conjunct cites this cell's, which just lost its generation). The
  cell's own WANTED stamp is deliberately *not* seeded: wantedness never
  depends on the cell's own generation (the design point of the
  location-keyed memo).
- **status flip to running** → clear the generation's own grounding stamp
  (I-G3); terminal flips seed nothing (the predicates are status-blind).

(d) Query correctness
=====================

:func:`load_component` + :func:`evaluate_component` compute the datalog
LFP restricted to the queried fact's justification component:

- The loader expands exactly the justification inputs of each unstamped
  fact it meets (grids → parent, deps, current-config asserter
  generations; task cells → attached targets and reader generations for
  ``wanted``, plus — only where ``grounded_task_cell`` is needed — the
  generation's own read subjects; data cells → grid and current-value
  writer generations; generations-as-would_start → their cell's wanted
  inputs and their grid), *pruning at stamped facts*, whose verdicts
  enter the LFP as constants. Pruning is sound by I-G2 (the stamp is the
  predicate's value); closure over unstamped facts is what makes the
  restricted LFP equal the global one: every clause body either lies in
  the component or is a stamped constant or is a *missing* fact
  (evaluated False, exactly as the datalog's failed existential).
- The LFP itself is a naive monotone iteration to fixpoint over the
  component's booleans — a direct transcription of the clauses (all
  positive, so the fixpoint exists and iteration reaches it).
- **Every evaluation derives from durable facts alone.** Requests are
  recorded at commitment (I-G1), so a cascade's in-flight demand
  structure is always in the tables the loader queries — the gates need
  no evaluation-time extras, every evaluation may write stamps, and
  negative stamps are as trustworthy as positive ones.
- **The stamp-prune covers only what the stamp memoizes.** A stamp
  memoizes one fact's *own* derived predicate (``grounded_grid(dep)``);
  it does not memoize per-pair relations that ride along the same rows —
  ``mint_compatible(referrer, dep)`` is recomputed fresh for every
  unstamped referrer, so the loader records ``config_ids`` for *every*
  grid row it loads, stamped or not (only the *expansion* of a stamped
  fact's justification inputs is pruned). Gating the config-id recording
  behind the stamp check was a real bug (an order-dependent false
  ``unanchored``, pinned by a regression test): the general trap is
  applying the prune to ride-along data the stamp never covered.
- ``mint_compatible(g, g')`` re-performs the structural checks grant
  minting performs, against the same immutable config entities:
  `view_spec.spec_from_binding` / `view_spec.spec_from_task_fn` over the
  live flattened axis kinds of ``g'``'s path, plus the declared-output
  membership check for task-output inputs and the handle-kind check for
  the ``task_fn`` slice. Config entities are content-addressed and
  immutable, so loading them mid-evaluation (between the two locked
  phases — see ``WorkflowSession``) cannot observe a torn state; the
  state-db rows are read at the phase-2 snapshot and only fingerprints
  already present at phase 1 are consulted.
- Tri-state mapping after the LFP: a fact the LFP derives is
  ``grounded``; otherwise it is ``orphaned`` when it has *no claimant at
  its current config/value* (grids: no matching sourcing record and no
  current-config assertion; data cells: no matching sourcing record and
  no current-value write claim; task cells: no attached target and no
  reader edge — the "locally unwanted" fact), else ``unanchored``
  (claimed, but the chain does not derive). ``unanchored`` is *not*
  predictive the way currency's unsettled is — there is no "resolves
  when X settles" semantics; it is simply non-derivability with
  claimants present.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment.change_hints import note_change
from fxtr_experiment.encoding import decode_key_path, encode_key_path
from fxtr_experiment_task.errors import ViewAxisError
from fxtr_experiment_task.model import (
    AxisKind,
    GridConfig,
    SliceBinding,
    SliceTemplate,
    TaskGridConfig,
)
from fxtr_experiment.storage import (
    OBS_KIND_WROTE_CELL,
    REQ_KIND_DATA,
    REQ_KIND_TASK_OUTPUT,
    id_of,
)
from fxtr_experiment_task.view_spec import spec_from_binding, spec_from_task_fn

Cell = tuple[str, str]  # (grid_path text, flattened-coords text)

GroundingVerdict = Literal["grounded", "unanchored", "orphaned"]
"""The tri-state stamp domain (see (d) in the module docstring)."""


@dataclass(frozen=True)
class GroundingReport:
    """One fact's grounding verdict at a single session snapshot.

    Attributes:
        verdict: The tri-state verdict of the queried fact (for a task
            cell: its current generation's ``grounded_task_cell``).
        reason: ``None`` iff grounded; otherwise the first failing
            conjunct found, naming the neighbor it failed on.
        wanted: For task cells: the cell's ``wanted`` verdict (`None`
            for grids and data/handle cells).
        wanted_reason: Diagnosis when ``wanted`` is False.
    """

    verdict: GroundingVerdict
    reason: str | None
    wanted: bool | None = None
    wanted_reason: str | None = None


# ---------------------------------------------------------------------------
# The component (loader output; evaluator input)
# ---------------------------------------------------------------------------


@dataclass
class _GridNode:
    kind: str
    config_id: FxtrID
    parent_path: str  # '[]' for top-level (the row-less root: vacuous)
    dep_paths: tuple[str, ...] = ()  # grid_refs, task grids only
    stamp: str | None = None
    stamp_reason: str | None = None  # the stamp's stored diagnosis
    user_claim: bool = False  # sourcing row matching current config
    asserters: list[Cell] = field(default_factory=list)  # gens @ current config


@dataclass
class _DCellNode:
    value_id: FxtrID
    stamp: str | None = None
    stamp_reason: str | None = None
    user_claim: bool = False  # sourcing row matching current value
    writers: list[Cell] = field(default_factory=list)  # gens @ current value


@dataclass
class _GenNode:
    exec_id: int
    status: str
    stamp: str | None = None
    stamp_reason: str | None = None
    reads_data: list[Cell] = field(default_factory=list)
    reads_out: list[Cell] = field(default_factory=list)

    @property
    def stamped(self) -> bool:
        return self.stamp is not None and self.status != "running"


@dataclass
class _TCellNode:
    """One task cell's wanted-side state (its generation, if any, is the
    ``gens`` entry at the same key)."""

    targeted: bool = False  # an attached target names it
    readers: list[Cell] = field(default_factory=list)  # gens with read_out edges
    wanted_stamp: str | None = None  # 'wanted' | 'unwanted' | None
    needs_grounded: bool = False  # grounded_task_cell required (root / cited)


@dataclass
class Component:
    """The justification component of one queried fact (see (d))."""

    root: tuple[str, ...]  # ('grid', path) | ('dcell'|'tcell', path, coords)
    grids: dict[str, "_GridNode | None"] = field(default_factory=dict)  # None = missing
    dcells: dict[Cell, "_DCellNode | None"] = field(default_factory=dict)
    tcells: dict[Cell, _TCellNode] = field(default_factory=dict)
    gens: dict[Cell, "_GenNode | None"] = field(default_factory=dict)
    config_ids: set[FxtrID] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Phase 1: component loading (one sync body; prunes at stamped facts)
# ---------------------------------------------------------------------------


def load_component(conn: sqlite3.Connection, root: tuple[str, ...]) -> Component:
    """Load the queried fact's justification component at one snapshot.

    Correctness (see (d)): the worklist expands exactly the justification
    inputs of every *unstamped* fact and stops at stamped ones (their
    verdicts join the LFP as constants, sound by I-G2). Facts referenced
    but absent load as ``None`` (the datalog's failed existential). Runs
    inside the caller's transaction; `WorkflowSession` holds the session
    lock across both phases.
    """
    if root[0] == "cell":
        # Resolve the generic cell root by its grid's kind (task cells
        # carry wanted + grounded_task_cell; everything else is a dcell).
        row = conn.execute("SELECT kind FROM grids WHERE key_path = ?", (root[1],)).fetchone()
        kind = "tcell" if row is not None and row[0] == "task" else "dcell"
        root = (kind, root[1], root[2])
    comp = Component(root=root)
    grid_frontier: list[str] = []
    dcell_frontier: list[Cell] = []
    tcell_frontier: list[tuple[Cell, bool]] = []  # (cell, needs_grounded)
    gen_frontier: list[Cell] = []  # gens needed as would_start

    if root[0] == "grid":
        grid_frontier.append(root[1])
    elif root[0] == "dcell":
        dcell_frontier.append((root[1], root[2]))
    else:  # tcell
        tcell_frontier.append(((root[1], root[2]), True))

    while grid_frontier or dcell_frontier or tcell_frontier or gen_frontier:
        while grid_frontier:
            path = grid_frontier.pop()
            if path in comp.grids:
                continue
            node = _load_grid(conn, path)
            comp.grids[path] = node
            if node is None:
                continue  # missing (the datalog's failed existential)
            # The config id is recorded even for stamped nodes: a stamped
            # dep contributes its verdict as a constant, but an unstamped
            # *referrer's* mint check still needs the dep's (and its
            # prefixes') config entities.
            comp.config_ids.add(node.config_id)
            if node.stamp is not None:
                continue  # stamped (constant — prune expansion)
            if node.parent_path != "[]":
                grid_frontier.append(node.parent_path)
            for dep in node.dep_paths:
                grid_frontier.append(dep)
                # mint_compatible walks the dep's whole prefix chain, so
                # prefix configs must be loadable even past stamped deps.
                for i in range(1, len(decode_key_path(dep))):
                    grid_frontier.append(encode_key_path(decode_key_path(dep)[:i]))
            gen_frontier.extend(node.asserters)
        while dcell_frontier:
            cell = dcell_frontier.pop()
            if cell in comp.dcells:
                continue
            node = _load_dcell(conn, cell)
            comp.dcells[cell] = node
            grid_frontier.append(cell[0])
            if node is None or node.stamp is not None:
                continue
            gen_frontier.extend(node.writers)
        while tcell_frontier:
            cell, needs_grounded = tcell_frontier.pop()
            existing = comp.tcells.get(cell)
            if existing is not None:
                if needs_grounded and not existing.needs_grounded:
                    existing.needs_grounded = True
                    gen_frontier.append(cell)  # ensure the gen row is present
                    _expand_gen_reads(conn, comp, cell, dcell_frontier, tcell_frontier)
                continue
            node = _load_tcell(conn, cell)
            node.needs_grounded = needs_grounded
            comp.tcells[cell] = node
            grid_frontier.append(cell[0])
            if node.wanted_stamp is None:
                gen_frontier.extend(node.readers)  # wanted's read_out inputs
            if needs_grounded:
                gen_frontier.append(cell)
                _expand_gen_reads(conn, comp, cell, dcell_frontier, tcell_frontier)
        while gen_frontier:
            cell = gen_frontier.pop()
            if cell in comp.gens:
                continue
            comp.gens[cell] = _load_gen(conn, cell)
            # would_start inputs: the gen's own cell's wanted + its grid.
            tcell_frontier.append((cell, False))
            grid_frontier.append(cell[0])
    return comp


def _expand_gen_reads(
    conn: sqlite3.Connection,
    comp: Component,
    cell: Cell,
    dcell_frontier: list[Cell],
    tcell_frontier: list[tuple[Cell, bool]],
) -> None:
    """Enqueue a generation's read subjects (only needed where its
    ``grounded_task_cell`` is derived — see the loader's aspect rule)."""
    gen = comp.gens.get(cell)
    if gen is None:
        gen = _load_gen(conn, cell)
        comp.gens[cell] = gen
    if gen is None or gen.stamped:
        return  # absent (False) or served from its stamp
    dcell_frontier.extend(gen.reads_data)
    tcell_frontier.extend((c, True) for c in gen.reads_out)


def _load_grid(conn: sqlite3.Connection, path: str) -> _GridNode | None:
    row = conn.execute(
        "SELECT kind, config_id, parent_path, grounding_verdict, grounding_reason"
        " FROM grids WHERE key_path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return None
    kind, config_blob, parent, stamp, reason = row
    node = _GridNode(
        kind=kind,
        config_id=id_of(config_blob),
        parent_path=parent,
        stamp=stamp,
        stamp_reason=reason,
    )
    if stamp is not None:
        return node
    node.dep_paths = tuple(
        r[0]
        for r in conn.execute(
            "SELECT referenced_path FROM grid_refs WHERE referrer_path = ?", (path,)
        ).fetchall()
    )
    node.user_claim = (
        conn.execute(
            "SELECT 1 FROM user_sourcing WHERE grid_path = ? AND coords = ''"
            " AND fingerprint = ?",
            (path, config_blob),
        ).fetchone()
        is not None
    )
    node.asserters = [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT te.grid_path, te.coords FROM subject_versions sv"
            " JOIN grid_assertions ga ON ga.version_id = sv.version_id"
            " JOIN task_execs te ON te.exec_id = ga.exec_id"
            " WHERE sv.grid_path = ? AND sv.coords = '' AND sv.output = ''"
            "   AND sv.fingerprint = ?",
            (path, config_blob),
        ).fetchall()
    ]
    return node


def _load_dcell(conn: sqlite3.Connection, cell: Cell) -> _DCellNode | None:
    row = conn.execute(
        "SELECT value_id, grounding_verdict, grounding_reason FROM cells"
        " WHERE grid_path = ? AND coords = ?",
        cell,
    ).fetchone()
    if row is None:
        return None
    value_blob, stamp, reason = row
    node = _DCellNode(value_id=id_of(value_blob), stamp=stamp, stamp_reason=reason)
    if stamp is not None:
        return node
    node.user_claim = (
        conn.execute(
            "SELECT 1 FROM user_sourcing WHERE grid_path = ? AND coords = ?" " AND fingerprint = ?",
            (*cell, value_blob),
        ).fetchone()
        is not None
    )
    node.writers = [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT te.grid_path, te.coords FROM subject_versions sv"
            " JOIN observations o ON o.version_id = sv.version_id AND o.kind = ?"
            " JOIN task_execs te ON te.exec_id = o.exec_id"
            " WHERE sv.grid_path = ? AND sv.coords = ? AND sv.output = ''"
            "   AND sv.fingerprint = ?",
            (OBS_KIND_WROTE_CELL, *cell, value_blob),
        ).fetchall()
    ]
    return node


def _load_tcell(conn: sqlite3.Connection, cell: Cell) -> _TCellNode:
    node = _TCellNode()
    row = conn.execute(
        "SELECT verdict FROM wanted_stamps WHERE grid_path = ? AND coords = ?", cell
    ).fetchone()
    if row is not None:
        node.wanted_stamp = row[0]
        return node
    node.targeted = (
        conn.execute(
            "SELECT 1 FROM targets t JOIN grids g ON g.key_path = t.grid_path"
            " WHERE t.grid_path = ? AND t.coords = ? AND t.expected_config = g.config_id",
            cell,
        ).fetchone()
        is not None
    )
    # Readers are *requesters* (I-G1: read_out is a request-record fact) —
    # a running cascade's demand is visible the moment it commits.
    node.readers = [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT DISTINCT te.grid_path, te.coords FROM request_edges re"
            " JOIN task_execs te ON te.exec_id = re.exec_id"
            " WHERE re.grid_path = ? AND re.coords = ? AND re.kind = ?",
            (*cell, REQ_KIND_TASK_OUTPUT),
        ).fetchall()
    ]
    return node


def _load_gen(conn: sqlite3.Connection, cell: Cell) -> _GenNode | None:
    row = conn.execute(
        "SELECT exec_id, status, grounding_verdict, grounding_reason FROM task_execs"
        " WHERE grid_path = ? AND coords = ?",
        cell,
    ).fetchone()
    if row is None:
        return None
    exec_id, status, stamp, reason = row
    node = _GenNode(exec_id=exec_id, status=status, stamp=stamp, stamp_reason=reason)
    if node.stamped:
        return node
    # Input conjuncts range over the *requested* set (I-G1) — for
    # non-complete generations a superset of resolved reads, and the more
    # faithful one (an aborted request is still what a replay re-issues).
    for g, c, kind in conn.execute(
        "SELECT grid_path, coords, kind FROM request_edges WHERE exec_id = ?",
        (exec_id,),
    ).fetchall():
        if kind == REQ_KIND_TASK_OUTPUT:
            node.reads_out.append((g, c))
        else:
            node.reads_data.append((g, c))
    return node


# ---------------------------------------------------------------------------
# Phase 2: the LFP + tri-state mapping + stamps (one sync body)
# ---------------------------------------------------------------------------


def evaluate_component(
    conn: sqlite3.Connection,
    comp: Component,
    configs: Mapping[FxtrID, GridConfig],
) -> GroundingReport | None:
    """Run the LFP over the loaded component, stamp derived verdicts, and
    return the root's report.

    Correctness (see (d)): a direct transcription of the datalog's
    clauses as a monotone iteration; stamped facts are constants; missing
    facts are False. Returns ``None`` when the root fact does not exist
    (nothing to judge — e.g. an empty cell). Must run under the same
    session lock as the load (I-G3); the config entities are immutable,
    so their mid-lock load cannot tear.
    """
    with conn:  # the stamps must COMMIT with the derivation (I-G3)
        lfp = _Lfp(comp, configs)
        lfp.solve()
        _write_stamps(conn, comp, lfp)
        return lfp.root_report()


class _Lfp:
    """The naive monotone iteration + the reason/verdict mapping."""

    def __init__(self, comp: Component, configs: Mapping[FxtrID, GridConfig]) -> None:
        self.comp = comp
        self.configs = configs
        self.grid_ok: dict[str, bool] = {}
        self.dcell_ok: dict[Cell, bool] = {}
        self.tcell_ok: dict[Cell, bool] = {}  # grounded_task_cell
        self.wanted: dict[Cell, bool] = {}
        self._compat_memo: dict[tuple[str, str], str | None] = {}
        for path, g in comp.grids.items():
            self.grid_ok[path] = g is not None and g.stamp == "grounded"
        for cell, d in comp.dcells.items():
            self.dcell_ok[cell] = d is not None and d.stamp == "grounded"
        for cell, t in comp.tcells.items():
            self.wanted[cell] = t.wanted_stamp == "wanted"
            gen = comp.gens.get(cell)
            self.tcell_ok[cell] = gen is not None and gen.stamp == "grounded"

    # -- the clauses (each returns the body's value at the current bounds) --

    def _would_start(self, cell: Cell) -> bool:
        gen = self.comp.gens.get(cell)
        return gen is not None and self.wanted.get(cell, False) and self.grid_ok.get(cell[0], False)

    def _eval_wanted(self, cell: Cell, t: _TCellNode) -> bool:
        return t.targeted or any(self._would_start(r) for r in t.readers)

    def _eval_grid(self, path: str, g: _GridNode) -> bool:
        claimed = g.user_claim or any(self._would_start(e) for e in g.asserters)
        if not claimed:
            return False
        if g.parent_path != "[]" and not self.grid_ok.get(g.parent_path, False):
            return False
        for dep in g.dep_paths:
            if not self.grid_ok.get(dep, False):
                return False
            if self._mint_incompat(path, dep) is not None:
                return False
        return True

    def _eval_dcell(self, cell: Cell, d: _DCellNode) -> bool:
        if not self.grid_ok.get(cell[0], False):
            return False
        return d.user_claim or any(self._would_start(e) for e in d.writers)

    def _eval_tcell(self, cell: Cell, gen: _GenNode) -> bool:
        if not self._would_start(cell):
            return False
        if all(self.dcell_ok.get(rd, False) for rd in gen.reads_data):
            return all(self.tcell_ok.get(rt, False) for rt in gen.reads_out)
        return False

    def solve(self) -> None:
        """Iterate the clauses to fixpoint (monotone: only False→True)."""
        changed = True
        while changed:
            changed = False
            for cell, t in self.comp.tcells.items():
                if t.wanted_stamp is None and not self.wanted[cell]:
                    if self._eval_wanted(cell, t):
                        self.wanted[cell] = changed = True
            for path, g in self.comp.grids.items():
                if g is not None and g.stamp is None and not self.grid_ok[path]:
                    if self._eval_grid(path, g):
                        self.grid_ok[path] = changed = True
            for cell, d in self.comp.dcells.items():
                if d is not None and d.stamp is None and not self.dcell_ok[cell]:
                    if self._eval_dcell(cell, d):
                        self.dcell_ok[cell] = changed = True
            for cell, t in self.comp.tcells.items():
                if not t.needs_grounded or self.tcell_ok[cell]:
                    continue
                gen = self.comp.gens.get(cell)
                if gen is not None and not gen.stamped and self._eval_tcell(cell, gen):
                    self.tcell_ok[cell] = changed = True

    # -- mint compatibility (the same structural checks minting performs) --

    def _mint_incompat(self, path: str, dep: str) -> str | None:
        """None if compatible; else the first structural failure found."""
        key = (path, dep)
        if key in self._compat_memo:
            return self._compat_memo[key]
        result = self._mint_incompat_uncached(path, dep)
        self._compat_memo[key] = result
        return result

    def _mint_incompat_uncached(self, path: str, dep: str) -> str | None:
        g = self.comp.grids.get(path)
        assert g is not None  # callers hold the node
        config = self.configs.get(g.config_id)
        if not isinstance(config, TaskGridConfig):
            return None  # non-task grids have no rule (vacuous)
        dep_node = self.comp.grids.get(dep)
        if dep_node is None:
            return f"rule references missing grid {dep}"
        flat_kinds = self._flat_kinds(dep)
        if flat_kinds is None:
            return f"a grid on the path to {dep} is missing"
        dep_config = self.configs.get(dep_node.config_id)
        slices: list[tuple[str, SliceTemplate, SliceBinding | None]] = []
        rule = config.rule
        if encode_key_path(tuple(s.key for s in rule.task_fn.path)) == dep:
            slices.append(("task_fn", rule.task_fn, None))
        for name, binding in rule.inputs.items():
            if isinstance(binding, SliceBinding):
                if encode_key_path(tuple(s.key for s in binding.slice.path)) == dep:
                    slices.append((name, binding.slice, binding))
        for name, slice_, binding in slices:
            if name == "task_fn":
                if dep_node.kind != "handle":
                    return f"task_fn slice targets a {dep_node.kind!r} grid"
                try:
                    spec_from_task_fn(slice_)
                except ViewAxisError as exc:
                    return f"task_fn slice cannot mint against {dep}: {exc}"
                continue
            assert binding is not None
            if slice_.output is not None:
                if not isinstance(dep_config, TaskGridConfig):
                    return f"input {name!r} selects an output of non-task grid {dep}"
                if slice_.output not in dep_config.outputs:
                    return f"input {name!r} selects undeclared output {slice_.output!r} of {dep}"
            try:
                spec_from_binding(
                    binding,
                    target_kind=dep_node.kind,  # type: ignore[arg-type]
                    flat_kinds=flat_kinds,
                )
            except ViewAxisError as exc:
                return f"input {name!r} cannot mint against {dep}: {exc}"
        return None

    def _flat_kinds(self, dep: str) -> "tuple[AxisKind, ...] | None":
        """The dep's flattened axis kinds from live configs (None if a
        prefix grid is missing or its config entity is unavailable)."""
        keys = decode_key_path(dep)
        kinds: list[AxisKind] = []
        for i in range(1, len(keys) + 1):
            node = self.comp.grids.get(encode_key_path(keys[:i]))
            if node is None:
                return None
            cfg = self.configs.get(node.config_id)
            if cfg is None:
                return None  # stamped-pruned prefix: config not loaded
            kinds.extend(a.kind for a in cfg.axes)
        return tuple(kinds)

    # -- tri-state + reasons ------------------------------------------------

    def grid_verdict(self, path: str) -> tuple[GroundingVerdict, str | None]:
        g = self.comp.grids[path]
        assert g is not None and g.stamp is None
        if self.grid_ok[path]:
            return "grounded", None
        if not g.user_claim and not g.asserters:
            return "orphaned", "no sourcing record and no current-config assertion"
        for dep in g.dep_paths:
            if not self.grid_ok.get(dep, False):
                return "unanchored", f"rule reference {dep} is not grounded"
            bad = self._mint_incompat(path, dep)
            if bad is not None:
                return "unanchored", bad
        if g.parent_path != "[]" and not self.grid_ok.get(g.parent_path, False):
            return "unanchored", f"enclosing grid {g.parent_path} is not grounded"
        return "unanchored", self._claim_chain_reason(g.user_claim, g.asserters)

    def dcell_verdict(self, cell: Cell) -> tuple[GroundingVerdict, str | None]:
        d = self.comp.dcells[cell]
        assert d is not None and d.stamp is None
        if self.dcell_ok[cell]:
            return "grounded", None
        if not d.user_claim and not d.writers:
            return "orphaned", "no sourcing record and no current-value write claim"
        if not self.grid_ok.get(cell[0], False):
            return "unanchored", f"grid {cell[0]} is not grounded"
        return "unanchored", self._claim_chain_reason(d.user_claim, d.writers)

    def tcell_verdict(self, cell: Cell) -> tuple[GroundingVerdict, str | None]:
        t = self.comp.tcells[cell]
        gen = self.comp.gens.get(cell)
        assert gen is not None and not gen.stamped
        if self.tcell_ok[cell]:
            return "grounded", None
        if t.wanted_stamp is None and not t.targeted and not t.readers:
            return "orphaned", "locally unwanted: no target and no current reader"
        if not self._would_start(cell):
            if not self.wanted.get(cell, False):
                return "unanchored", "not wanted (no attached target; no reader would start)"
            return "unanchored", f"grid {cell[0]} is not grounded"
        for rd in gen.reads_data:
            if not self.dcell_ok.get(rd, False):
                return "unanchored", f"read data cell {rd[0]}{rd[1]} is not grounded"
        for rt in gen.reads_out:
            if not self.tcell_ok.get(rt, False):
                return "unanchored", f"read task cell {rt[0]}{rt[1]} is not grounded"
        raise AssertionError("tcell conjuncts all hold but the LFP did not derive it")

    def _claim_chain_reason(self, user_claim: bool, claimants: list[Cell]) -> str:
        assert not user_claim  # a user claim grounds outright once deps hold
        names = ", ".join(f"{g}{c}" for g, c in claimants[:3])
        return f"no claimant would start (claimants: {names})"

    def wanted_reason(self, cell: Cell) -> str | None:
        if self.wanted.get(cell, False):
            return None
        t = self.comp.tcells[cell]
        if not t.targeted and not t.readers:
            return "no attached target and no current reader"
        return "no attached target; no reader would start"

    def root_report(self) -> GroundingReport | None:
        root = self.comp.root
        if root[0] == "grid":
            g = self.comp.grids.get(root[1])
            if g is None:
                return None
            if g.stamp is not None:
                # A stamped verdict serves its stored diagnosis (the stamp
                # columns carry both; I-G2 keeps them current together).
                return GroundingReport(g.stamp, g.stamp_reason)  # type: ignore[arg-type]
            verdict, reason = self.grid_verdict(root[1])
            return GroundingReport(verdict, reason)
        if root[0] == "dcell":
            cell = (root[1], root[2])
            d = self.comp.dcells.get(cell)
            if d is None:
                return None
            if d.stamp is not None:
                return GroundingReport(d.stamp, d.stamp_reason)  # type: ignore[arg-type]
            verdict, reason = self.dcell_verdict(cell)
            return GroundingReport(verdict, reason)
        cell = (root[1], root[2])
        t = self.comp.tcells.get(cell)
        if t is None:
            return None
        gen = self.comp.gens.get(cell)
        if gen is None:
            return None  # no generation: nothing to judge (wanted may still stamp)
        wanted = self.wanted.get(cell, False)
        w_reason = self.wanted_reason(cell)
        if gen.stamped:
            assert gen.stamp is not None
            return GroundingReport(
                gen.stamp,  # type: ignore[arg-type]
                gen.stamp_reason,
                wanted=wanted,
                wanted_reason=w_reason,
            )
        verdict, reason = self.tcell_verdict(cell)
        return GroundingReport(verdict, reason, wanted=wanted, wanted_reason=w_reason)


def _write_stamps(conn: sqlite3.Connection, comp: Component, lfp: _Lfp) -> None:
    """Stamp every derived fact's verdict (I-G3: same transaction as the
    walk; running generations' own verdicts are skipped; served-from-stamp
    facts untouched)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for path, g in comp.grids.items():
        if g is None or g.stamp is not None:
            continue
        verdict, reason = lfp.grid_verdict(path)
        conn.execute(
            "UPDATE grids SET grounding_verdict = ?, grounding_reason = ?,"
            " grounding_as_of = ? WHERE key_path = ?",
            (verdict, reason, now, path),
        )
    for cell, d in comp.dcells.items():
        if d is None or d.stamp is not None:
            continue
        verdict, reason = lfp.dcell_verdict(cell)
        conn.execute(
            "UPDATE cells SET grounding_verdict = ?, grounding_reason = ?,"
            " grounding_as_of = ? WHERE grid_path = ? AND coords = ?",
            (verdict, reason, now, *cell),
        )
    for cell, t in comp.tcells.items():
        if t.wanted_stamp is None:
            wanted = lfp.wanted.get(cell, False)
            conn.execute(
                "INSERT OR REPLACE INTO wanted_stamps"
                " (grid_path, coords, verdict, reason, as_of) VALUES (?, ?, ?, ?, ?)",
                (*cell, "wanted" if wanted else "unwanted", lfp.wanted_reason(cell), now),
            )
        gen = comp.gens.get(cell)
        if not t.needs_grounded or gen is None or gen.stamped or gen.status == "running":
            continue
        verdict, reason = lfp.tcell_verdict(cell)
        conn.execute(
            "UPDATE task_execs SET grounding_verdict = ?, grounding_reason = ?,"
            " grounding_as_of = ? WHERE exec_id = ?",
            (verdict, reason, now, gen.exec_id),
        )


# ---------------------------------------------------------------------------
# Marking (stamp clearing; runs inside each mutating body's transaction)
# ---------------------------------------------------------------------------

Seed = tuple[str, ...]
"""A marking seed: ('grid', path) | ('dcell', path, coords) |
('wanted', path, coords) | ('gen', path, coords)."""


def mark_grounding(conn: sqlite3.Connection, seeds: Iterable[Seed]) -> None:
    """Clear the grounding stamps of every stamped transitive dependent of
    the seeded facts (I-G4).

    Citer ("who consults me") edges per fact kind, straight from the
    clauses:

    - GRID(g): the data cells of g; child grids (parent clause); referrer
      grids (dep clause); and — via would_start of every generation *at*
      g — the fan below.
    - DCELL(d): the generations that read d (their input conjunct).
    - WANTED(c): via would_start of c's generation — the fan below.
    - GEN(c) (= grounded_task_cell(c)): the generations that read c's
      outputs.

    **The would_start fan** (applied when a WANTED or GRID input of a
    generation's would_start is cleared, regardless of that generation's
    own stamp state — would_start itself is never stamped, its value
    lives inside its dependents): the generation's read_out subjects'
    WANTED stamps, its written cells' DCELL stamps, its asserted grids'
    GRID stamps, and its own GEN stamp.

    Pruning: a seed whose stamp is already absent is not recursed from —
    sound by the prune invariant P (no stamped fact depends on an
    unstamped one). Termination: each stamped fact is cleared at most
    once per walk.

    Events: ``("cells", grid)`` for every grid whose stamps this walk
    actually clears (any kind — a cleared stamp is exactly "a displayed
    verdict may change"), noted into the ambient collector
    (`change_hints.note_change`); the enclosing session op owns the
    flush.
    """
    frontier: list[Seed] = list(seeds)
    while frontier:
        seed = frontier.pop()
        kind = seed[0]
        if kind == "grid":
            path = seed[1]
            if not _clear_grid_stamp(conn, path):
                continue
            note_change("cells", path)
            for (coords,) in conn.execute(
                "SELECT coords FROM cells WHERE grid_path = ?", (path,)
            ).fetchall():
                frontier.append(("dcell", path, coords))
            for (child,) in conn.execute(
                "SELECT key_path FROM grids WHERE parent_path = ?", (path,)
            ).fetchall():
                frontier.append(("grid", child))
            for (referrer,) in conn.execute(
                "SELECT referrer_path FROM grid_refs WHERE referenced_path = ?", (path,)
            ).fetchall():
                frontier.append(("grid", referrer))
            for g, c in conn.execute(
                "SELECT grid_path, coords FROM task_execs WHERE grid_path = ?", (path,)
            ).fetchall():
                _fan_would_start(conn, (g, c), frontier)
        elif kind == "dcell":
            if not _clear_dcell_stamp(conn, (seed[1], seed[2])):
                continue
            note_change("cells", seed[1])
            for g, c in _readers_of(conn, seed[1], seed[2], task_outputs=False):
                frontier.append(("gen", g, c))
        elif kind == "wanted":
            if not _clear_wanted_stamp(conn, (seed[1], seed[2])):
                continue
            note_change("cells", seed[1])
            _fan_would_start(conn, (seed[1], seed[2]), frontier)
        else:  # gen
            if not _clear_gen_stamp(conn, (seed[1], seed[2])):
                continue
            note_change("cells", seed[1])
            for g, c in _readers_of(conn, seed[1], seed[2], task_outputs=True):
                frontier.append(("gen", g, c))


def would_start_fan_seeds(conn: sqlite3.Connection, cell: Cell) -> list[Seed]:
    """The departing-record fan for a generation about to be deleted
    (turnover / grid delete): the same dependents `_fan_would_start`
    reaches, computed *before* the rows vanish. The caller applies them
    via :func:`mark_grounding` after the deletion."""
    seeds: list[Seed] = [("gen", *cell)]
    row = conn.execute(
        "SELECT exec_id FROM task_execs WHERE grid_path = ? AND coords = ?", cell
    ).fetchone()
    if row is None:
        return seeds
    (exec_id,) = row
    for g, c in conn.execute(
        "SELECT grid_path, coords FROM request_edges WHERE exec_id = ? AND kind = ?",
        (exec_id, REQ_KIND_TASK_OUTPUT),
    ).fetchall():
        seeds.append(("wanted", g, c))
    for g, c in conn.execute(
        "SELECT sv.grid_path, sv.coords FROM observations o"
        " JOIN subject_versions sv ON sv.version_id = o.version_id"
        " WHERE o.exec_id = ? AND o.kind = ?",
        (exec_id, OBS_KIND_WROTE_CELL),
    ).fetchall():
        seeds.append(("dcell", g, c))
    for (g,) in conn.execute(
        "SELECT DISTINCT sv.grid_path FROM grid_assertions ga"
        " JOIN subject_versions sv ON sv.version_id = ga.version_id"
        " WHERE ga.exec_id = ?",
        (exec_id,),
    ).fetchall():
        seeds.append(("grid", g))
    # Readers of this cell's outputs cite its grounded_task_cell.
    for g, c in _readers_of(conn, *cell, task_outputs=True):
        seeds.append(("gen", g, c))
    return seeds


def _fan_would_start(conn: sqlite3.Connection, cell: Cell, frontier: list[Seed]) -> None:
    """Fan a would_start input change to the generation's dependents (see
    :func:`mark_grounding`'s docstring; no prune on the gen's own stamp)."""
    row = conn.execute(
        "SELECT exec_id FROM task_execs WHERE grid_path = ? AND coords = ?", cell
    ).fetchone()
    if row is None:
        return
    (exec_id,) = row
    frontier.append(("gen", *cell))
    for g, c in conn.execute(
        "SELECT grid_path, coords FROM request_edges WHERE exec_id = ? AND kind = ?",
        (exec_id, REQ_KIND_TASK_OUTPUT),
    ).fetchall():
        frontier.append(("wanted", g, c))
    for g, c in conn.execute(
        "SELECT sv.grid_path, sv.coords FROM observations o"
        " JOIN subject_versions sv ON sv.version_id = o.version_id"
        " WHERE o.exec_id = ? AND o.kind = ?",
        (exec_id, OBS_KIND_WROTE_CELL),
    ).fetchall():
        frontier.append(("dcell", g, c))
    for (g,) in conn.execute(
        "SELECT DISTINCT sv.grid_path FROM grid_assertions ga"
        " JOIN subject_versions sv ON sv.version_id = ga.version_id"
        " WHERE ga.exec_id = ?",
        (exec_id,),
    ).fetchall():
        frontier.append(("grid", g))


def _readers_of(
    conn: sqlite3.Connection, grid_path: str, coords: str, *, task_outputs: bool
) -> list[Cell]:
    """Generations with request edges at a location (task-output demands
    only, or data/handle reads only)."""
    kind = REQ_KIND_TASK_OUTPUT if task_outputs else REQ_KIND_DATA
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT DISTINCT te.grid_path, te.coords FROM request_edges re"
            " JOIN task_execs te ON te.exec_id = re.exec_id"
            " WHERE re.grid_path = ? AND re.coords = ? AND re.kind = ?",
            (grid_path, coords, kind),
        ).fetchall()
    ]


def _clear_grid_stamp(conn: sqlite3.Connection, path: str) -> bool:
    cur = conn.execute(
        "UPDATE grids SET grounding_verdict = NULL, grounding_reason = NULL,"
        " grounding_as_of = NULL WHERE key_path = ? AND grounding_verdict IS NOT NULL",
        (path,),
    )
    return cur.rowcount > 0


def _clear_dcell_stamp(conn: sqlite3.Connection, cell: Cell) -> bool:
    cur = conn.execute(
        "UPDATE cells SET grounding_verdict = NULL, grounding_reason = NULL,"
        " grounding_as_of = NULL WHERE grid_path = ? AND coords = ?"
        " AND grounding_verdict IS NOT NULL",
        cell,
    )
    return cur.rowcount > 0


def _clear_wanted_stamp(conn: sqlite3.Connection, cell: Cell) -> bool:
    cur = conn.execute("DELETE FROM wanted_stamps WHERE grid_path = ? AND coords = ?", cell)
    return cur.rowcount > 0


def _clear_gen_stamp(conn: sqlite3.Connection, cell: Cell) -> bool:
    cur = conn.execute(
        "UPDATE task_execs SET grounding_verdict = NULL, grounding_reason = NULL,"
        " grounding_as_of = NULL WHERE grid_path = ? AND coords = ?"
        " AND grounding_verdict IS NOT NULL",
        cell,
    )
    return cur.rowcount > 0


def clear_gen_grounding_stamp(conn: sqlite3.Connection, exec_id: int) -> None:
    """The status-flip-to-running self-mark (no propagation — see (c))."""
    conn.execute(
        "UPDATE task_execs SET grounding_verdict = NULL, grounding_reason = NULL,"
        " grounding_as_of = NULL WHERE exec_id = ?",
        (exec_id,),
    )


def delete_wanted_stamps_under(conn: sqlite3.Connection, grid_path: str) -> None:
    """Grid deletion: drop the wanted memo rows for locations that no
    longer exist (their grid is going away in the same transaction)."""
    conn.execute("DELETE FROM wanted_stamps WHERE grid_path = ?", (grid_path,))
