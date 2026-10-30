"""Workflow state-db schema (sqlite DDL) — the durable half of the runner.

Storage is two files side by side: this **state db** (custom schema below)
and the fxtr **entity store** (an fxtr2 sqlite store). The liveness
invariant ties them: an FxtrID may be recorded here only *after* its
closure is persisted (and pinned — pins are the entity store's GC roots;
pin key = the id itself, idempotent) in the entity store, so this db never
dangles. v0 retains all entities; real GC arrives with the version log.

**Identity encoding.** Grid and cell identity columns are canonical JSON
text (strict encoding: no spaces, sorted keys; we own every conversion):

- ``grids.key_path`` — the *key-level* path: a canonical JSON array of
  key strings from the root, e.g. ``["mh","grades","judge_calls"]``, with
  **no coordinates** (configs attach to subtree keys, P1, so one row
  spans every cell of the enclosing subtrees) and **no step-kind
  discriminators** — whether a step is subtree or task-internals descent
  is derivable from the parent grid's kind (task grids only have
  internals children). A kind-swapping delete-recreate at the same keys
  cannot confuse observers: their ``grid_config`` observations
  fingerprint the config, which encodes the kind, so they go stale
  regardless. (Model-level paths keep the typed step variants; this is
  storage encoding only.)
- ``coords`` columns — the **full flattened coordinate tuple** (every
  enclosing subtree's coordinates, then the grid's own) as one JSON array
  of str|int. Storage is thus literally the "prepend the subtree's
  columns" flattened-table view; slice queries are prefix filters.

Path-keyed identity is semantic: linkage subjects reference paths, so
deleting a grid and recreating it at the same key with identical values
resurrects its observers (the revert-resurrection property). Child lookup
goes through the denormalized ``parent_path`` column (indexed) — serving
``list_keys``, the rmdir-style delete emptiness check, and future
recursive walks via parent chains; JSON-prefix text matching is never
needed anywhere.

**Generations are the observer identity.** ``task_execs`` has PK
``generation`` (a globally unique id) + UNIQUE ``(grid_path, coords)`` —
exactly one *current* generation per cell; old generations are deleted at
turnover, and every generation-scoped table (results, checkpoints,
observations, grid assertions) hangs off the FK with ``ON DELETE
CASCADE``, making turnover a single row delete — a generation's *claims*
(the values it wrote, the grids it asserted) expire with it. History is
the version log's job (later).

**Observed subjects are interned as *versions*.** "A thing observed" is a
(location, fingerprint) pair — a **subject version** — interned in
``subject_versions`` (unique on location + fingerprint, with ``''``
sentinels rather than NULLs for absent coords/output — sqlite treats
NULLs as distinct in unique indexes; the same index serves
reverse-by-location queries as a prefix scan). Many floating versions of
one location may coexist; which one is *current* is determined by what
the location actually holds now — which is exactly the direct-staleness
check of the currency model: a generation is stale iff it references a
version whose location settled on a different value. ``observations`` is then a pure bipartite edge set
``(generation, version_id, kind)`` between observers and observed facts:
the fingerprint is stored once per distinct observed value, not once per
observer (the common synchronized fan-in — every cell of a task grid
observing the shared task_fn/config cells — writes one version row plus
tiny edges). ``kind`` stays on the edge: writing and reading the same
value are different *relations* to the same fact. The op record's
one-entry-per-(generation, location) set invariant is enforced by the
in-memory record during runs (and re-verified by the safety pass), not by
the schema — the PK only forbids duplicate edges. Version rows are pure
interning: never load-bearing to delete (unreferenced old versions are
opportunistically sweepable), and delete-recreate with identical values
re-interns to the same version, so observers resurrect.

Per-kind version contract (one table, kind-discriminated — the two hot
queries want one index each: record-loading hits the ``(generation, …)``
PK prefix, staleness reversal prefix-scans the identity index):

- ``grid_config``: ``coords = ''``, ``output = ''`` (the subject is the
  grid itself); fingerprint = config entity id.
- ``wrote_cell``: coords set, ``output = ''``; fingerprint = written id.
- ``cell_value``: coords set; ``output`` set iff the subject is a task
  grid; fingerprint = the value/frame/key id (which is also what a replay
  hit serves).

**Requests are recorded at commitment.** ``request_edges`` is the demand
record grounding derives from (``notes/grounding.md``): one row per
(generation, requested location), written at record-**miss** time
*before* the op goes live — so the row exists even when the request
never resolves (a fault, or a crash mid-request), which is exactly what
lets a fresh cascade's wantedness derive from durable state while
nothing has resolved yet. Location-keyed and fingerprint-blind (no
``subject_versions`` join — grounding never asks about values), with an
output-ness ``kind`` flag: `wanted` reads the task-output rows;
``grounded_task_cell``'s input conjuncts read all rows (the *requested*
set — a superset of resolved reads only for non-complete generations,
and the more faithful demand estimate). INSERT OR IGNORE keeps
aborted-then-resumed requests idempotent (a record hit implies the edge
exists from the original miss); frozen engines never miss, so the
safety pass stays write-free — and at completion it *compares* this set
against the recorded reads: every request answered, every read
requested (see ``notes/execution_semantics.md``). Rows expire with the generation
(cascade); like intent records, the subject path is deliberately
un-FK'd — a request of a since-deleted grid remains a fact of the
generation's record.

**Failures are stored, plural, raise-side.** ``failures`` holds every
failure observed inside a generation's runs — concurrent branches of one
run may each fault or error — written at raise time by the raising side
(the engine for op failures; the executor for exceptions escaping a run
with no engine-recorded failure). A ``running`` generation with failure
rows is the well-defined *poisoned* state; at run end the terminal status
derives from the set (any error-kind row → ``errored``, else
``interrupted``). The ``kind`` column is recorded explicitly from the
typed exception at raise time — classification is never re-derived from
``error_class`` text, and never parsed from wire-crossed exceptions.

**Currency stamps.** ``task_execs`` carries the cached currency verdict —
the *stamp*: ``currency_verdict`` / ``currency_reason`` /
``currency_as_of``, NULL together (NULL verdict ⇔ dirty; there is no
separate dirty bit — "dirty" and "unstamped" provably never come apart).
The maintenance invariant (**I1**): *any settled, currency-relevant
change that could alter a stamped generation's verdict clears that
stamp, in the same transaction as the change* — so a live stamp on a
settled generation is always current and serves with no walk. Stamps are
written only by verification (`currency.evaluate_cell`, via
``WorkflowSession.cell_currency``) and only onto settled
(terminal-status) generations; running generations always derive live.
Mutation bodies in `state_store.py` each declare what they mark
("Marks:" in their docstrings); the trigger table and the soundness
argument for prune-at-unstamped propagation live in
``notes/execution_semantics.md``. The reverse index the marking walk
needs is ``subject_versions_identity`` (its ``(grid_path, coords)``
prefix) + ``observations_by_version``.

**Grounding stamps.** The derived predicates of ``notes/grounding.md``
are cached like currency: ``grounding_verdict`` / ``grounding_reason`` /
``grounding_as_of`` on ``grids`` (grounded_grid), ``cells``
(grounded_data_cell), and ``task_execs`` (grounded_task_cell, the
current generation's), NULL together and NULL = unstamped; `wanted` has
its own location-keyed memo table (``wanted_stamps`` — see its DDL
comment for why it is deliberately not generation-scoped). Verdicts are
the tri-state ``grounded | unanchored | orphaned`` (orphaned = the
depth-1 no-claimant fact; unanchored = claimed but not derivable —
*not* predictive the way currency's unsettled is). The maintenance
invariants, the state ↔ datalog correspondence, and the marking trigger
table live in `grounding.py`'s module docstring; the mutation bodies in
`state_store.py` each declare their grounding marks alongside their
currency "Marks:" lines.

**Sandboxed bindings are durable records** (``sandbox_bindings`` +
``sandbox_binding_deps``; ``notes/sandboxed_bindings.md``). A sandboxed
binding answers "what nix service serves this handle key" with a defn
triple + pinned dependency map + optional JSON config, keyed by the
handle *key id* (not a cell). It is deliberately **not identity** — no
handle fingerprint, no currency/grounding marks — so swapping one never
invalidates results (the compatibility ratchet). Two design choices worth
noting: the dependency map is *normalized* into its own table (one row
per name, unordered — a canonical ``dep_name`` sort supplies stable order
for export/listing) so breakage is reason-able bidirectionally; and
``config_json`` is stored as canonical JSON *text* rather than an entity
(it is human-readable lockfile data, not identity, and never pins a
closure). Cross-class exclusivity (a key is bound in at most one of the
ephemeral / sandboxed classes) is enforced above this layer, in the
capability (the ephemeral class is in-memory runner state — see below).

**What is deliberately absent.** No checkpoint-ownership table (ownership
is structural: a checkpoint grid's key-path passes through the owning
task grid's internals step; the owning cell is the flattened-coords
prefix up to that step). No target-attachment / active-flag
columns (derived on demand). No view tokens and no *ephemeral* handle
bindings (in-memory runner state; the *sandboxed* class, by contrast, is
durable — above), no intent set beyond the sourcing/target tables.

**Blobby values are entity ids** (the uniform rule): configs
(``config_id``), checkpoint args (``args_id`` — entity-encoding pins the
closures the args reference, which an inline blob would silently not do),
target slice templates (``slice_id``), all frame values.

**Intent records are location-keyed and deliberately un-FK'd.**
``user_sourcing`` rows (revocable "I assert this grid's config / this
cell's value" pins — the editable root facts of ``notes/grounding.md``)
and ``targets`` are intent, not data: they reference grids by path text
with **no** FK to ``grids``, so they survive grid deletion as *dangling*
records (derived at read time by comparing the pinned fingerprint against
live state; never silently rebound or dropped). The **grid-before-cell
invariant** is maintained, not merely checked at add time: a cell-level
intent row (cell sourcing, target) requires the grid-level sourcing row
for its path at all times — adds check it, and revoking a grid's sourcing
refuses while dependents exist unless the same transaction cascades them
(app-enforced in `state_store.py`; not expressible as an FK since the
grid row is the ``coords = ''`` row of the same table).

**Public vs private identity.** App-level identities that may leave the db
are uuids or location tuples: ``task_execs.generation`` (app-minted uuid;
events, UI, the version log later) and a fact's (location, fingerprint).
Every *db-internal* reference goes through a compact private INTEGER
surrogate — ``exec_id``, ``version_id`` — that must never be exported:
sqlite may reuse rowids after deletes, and FK cascades guarantee no
dangling private refs exist *inside* the db, so reuse is harmless exactly
as long as the ids stay inside. Small closed enums (observation
kind) are stored as ints with named constants in this module;
``status`` stays text (five values, one row per cell, read raw
constantly).

**Transaction discipline.** Single writer (v0 single-runner), WAL, one
transaction per effectful op (the outcome and its observation row commit
atomically); generation turnover, ``clear``, and ``delete`` are each one
transaction. During a run the record-check consults the generation's
record *in memory*; these rows exist for crash durability,
cross-generation staleness queries, and the UI. Connections must set
``PRAGMA foreign_keys = ON`` (sqlite default is off).
"""

from __future__ import annotations

import sqlite3

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment_task.model import FailureKind, ObservationKind, RequestKind

SCHEMA_VERSION = 8

# Observation kinds (observations.kind).
OBS_KIND_CELL_VALUE = 0
OBS_KIND_WROTE_CELL = 1
OBS_KIND_GRID_CONFIG = 2

# Request-edge kinds (request_edges.kind) — the subject's output-ness.
# Fixed per location (a cell is either a task cell or a data/handle cell),
# so the flag never conflicts under the (exec_id, location) PK.
REQ_KIND_TASK_OUTPUT = 0
REQ_KIND_DATA = 1

# Failure kinds (failures.kind) — the errored/interrupted split, recorded
# at raise time from the typed exception (CellFaultError → fault).
FAILURE_ERROR = 0
FAILURE_FAULT = 1

# The kind codes are storage bookkeeping; the at-rest/entity forms use the
# name literals of `model.py` (the generation record never stores the
# integers). These maps are the one conversion point, each way.
OBS_KIND_NAMES: dict[int, ObservationKind] = {
    OBS_KIND_CELL_VALUE: "cell_value",
    OBS_KIND_WROTE_CELL: "wrote_cell",
    OBS_KIND_GRID_CONFIG: "grid_config",
}
OBS_KIND_CODES: dict[ObservationKind, int] = {v: k for k, v in OBS_KIND_NAMES.items()}
REQ_KIND_NAMES: dict[int, RequestKind] = {
    REQ_KIND_TASK_OUTPUT: "task_output",
    REQ_KIND_DATA: "data",
}
REQ_KIND_CODES: dict[RequestKind, int] = {v: k for k, v in REQ_KIND_NAMES.items()}
FAILURE_KIND_NAMES: dict[int, FailureKind] = {
    FAILURE_ERROR: "error",
    FAILURE_FAULT: "fault",
}
FAILURE_KIND_CODES: dict[FailureKind, int] = {v: k for k, v in FAILURE_KIND_NAMES.items()}

SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,   -- metadata key (e.g. 'schema_version')
        value TEXT NOT NULL     -- metadata value, as text
    ) STRICT
    """,
    """
    CREATE TABLE grids (
        -- One row per key-level grid (configs attach to subtree keys, P1).
        key_path TEXT PRIMARY KEY,  -- canonical JSON array of key strings from
                                    -- the root, e.g. '["mh","grades"]'. No
                                    -- coordinates; no step-kind discriminators
                                    -- (derivable: task parents only have
                                    -- internals children)
        parent_path TEXT NOT NULL,  -- the parent grid's key_path ('[]' for
                                    -- top-level). Denormalized from key_path;
                                    -- indexed for child lookup (list_keys, the
                                    -- rmdir delete check, recursive walks)
        kind TEXT NOT NULL,         -- this grid's kind: data|dataframe|handle|
                                    -- task|checkpoint|subtree
        config_id BLOB NOT NULL,    -- entity id of the canonical entity-encoded
                                    -- grid config; also the grid_config
                                    -- observation fingerprint
        grounding_verdict TEXT,     -- the grid's grounding stamp (grounded|
                                    -- unanchored|orphaned; NULL = unstamped;
                                    -- see 'Grounding stamps' in the module
                                    -- docstring)
        grounding_reason TEXT,      -- diagnosis; NULL iff grounded (or
                                    -- unstamped)
        grounding_as_of TEXT        -- stamp time (ISO; observability only)
    ) STRICT
    """,
    "CREATE INDEX grids_by_parent ON grids(parent_path)",
    """
    CREATE TABLE cells (
        -- Data / dataframe / handle cell contents.
        grid_path TEXT NOT NULL     -- grids.key_path of the containing grid
            REFERENCES grids(key_path) ON DELETE CASCADE,
        coords TEXT NOT NULL,       -- FULL flattened coordinate tuple (every
                                    -- enclosing subtree's coords, then the
                                    -- grid's own) as a canonical JSON array of
                                    -- str|int, e.g. '["v1","s1",3]'
        value_id BLOB NOT NULL,     -- entity id of the value: the cell entity
                                    -- (data), the canonical frame entity
                                    -- (dataframe), or the handle entity (handle)
        grounding_verdict TEXT,     -- the cell's grounding stamp (grounded|
                                    -- unanchored|orphaned; NULL = unstamped)
        grounding_reason TEXT,      -- diagnosis; NULL iff grounded/unstamped
        grounding_as_of TEXT,       -- stamp time (ISO; observability only)
        PRIMARY KEY (grid_path, coords)
    ) STRICT
    """,
    """
    CREATE TABLE task_execs (
        -- One row per task cell = its CURRENT generation. Turnover deletes and
        -- re-inserts; the FK cascades sweep every generation-scoped table.
        exec_id INTEGER PRIMARY KEY,    -- PRIVATE surrogate key: the FK target
                                        -- for every generation-scoped table
                                        -- (see 'Public vs private identity')
        generation TEXT NOT NULL,       -- PUBLIC globally unique id (uuid) of
                                        -- this logical execution — the
                                        -- exported identity (events, UI,
                                        -- version log); runs share it (see
                                        -- notes/execution_semantics.md)
        grid_path TEXT NOT NULL         -- the task grid this cell belongs to
            REFERENCES grids(key_path) ON DELETE CASCADE,
        coords TEXT NOT NULL,           -- the cell: flattened coords (JSON)
        status TEXT NOT NULL,           -- pending|running|complete|errored|
                                        -- interrupted (what went wrong lives
                                        -- in the failures table)
        current_fn BLOB NOT NULL,       -- task-fn handle entity id this
                                        -- execution is *current against*
                                        -- (handle migrations may evolve it)
        executed_fn BLOB NOT NULL,      -- task-fn handle entity id that
                                        -- actually ran; never rewritten
        currency_verdict TEXT,          -- the currency stamp (see module
                                        -- docstring): cached verdict
                                        -- fresh|stale|unsettled; NULL =
                                        -- dirty/unstamped. Written only by
                                        -- verification, only for settled
                                        -- generations; cleared by marking
        currency_reason TEXT,           -- the stamp's diagnosis; NULL iff
                                        -- fresh (or unstamped)
        currency_as_of TEXT,            -- when the stamp was written (ISO;
                                        -- observability only — correctness
                                        -- never consults it)
        grounding_verdict TEXT,         -- the current generation's grounding
                                        -- stamp (grounded_task_cell:
                                        -- grounded|unanchored|orphaned;
                                        -- NULL = unstamped; running
                                        -- generations never stamp)
        grounding_reason TEXT,          -- diagnosis; NULL iff grounded/
                                        -- unstamped
        grounding_as_of TEXT,           -- stamp time (ISO)
        record_id BLOB,                 -- entity id of the generation record
                                        -- (the at-rest form of everything
                                        -- this generation owns —
                                        -- notes/at_rest_format.md). The
                                        -- flip-time invariant: non-NULL ⇔
                                        -- status is terminal — a state
                                        -- terminal-with-NULL is
                                        -- unrepresentable (set_status's
                                        -- choreography persists the record
                                        -- before the one transaction that
                                        -- flips status + pointer; flips to
                                        -- running clear it; the startup
                                        -- crashed-run flip follows the
                                        -- same gapless choreography)
        UNIQUE (generation),
        UNIQUE (grid_path, coords)
    ) STRICT
    """,
    """
    CREATE TABLE failures (
        -- The poisoning record: failures observed inside a generation's
        -- runs, written at raise time by the raising side (see the module
        -- docstring). Plural per generation: concurrent branches may each
        -- fault or error. A running generation with rows here is poisoned;
        -- the terminal status derives from the set at run end.
        failure_id INTEGER PRIMARY KEY, -- PRIVATE surrogate; insertion order
                                        -- doubles as display order
        exec_id INTEGER NOT NULL        -- the poisoned generation
            REFERENCES task_execs(exec_id) ON DELETE CASCADE,
        kind INTEGER NOT NULL,          -- FAILURE_* constants: 0 error (the
                                        -- task's own fault → errored), 1
                                        -- fault (a not-ready dependency →
                                        -- interrupted). Recorded from the
                                        -- typed exception at raise time;
                                        -- never re-derived from error_class
        error_class TEXT NOT NULL,      -- the raised exception's class name
                                        -- (display only, never load-bearing)
        message TEXT NOT NULL,          -- human-readable failure message
        ts TEXT NOT NULL                -- wall-clock timestamp (ISO)
    ) STRICT
    """,
    "CREATE INDEX failures_by_exec ON failures(exec_id)",
    """
    CREATE TABLE task_results (
        -- Per-output results of a complete generation.
        exec_id INTEGER NOT NULL
            REFERENCES task_execs(exec_id) ON DELETE CASCADE,
        output TEXT NOT NULL,       -- declared output name (e.g. 'scores')
        value_id BLOB NOT NULL,     -- entity id (EntityOutput) or canonical
                                    -- frame entity id (FrameOutput)
        PRIMARY KEY (exec_id, output)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoints (
        -- Durable step entries. Ownership is structural: the checkpoint
        -- grid's key-path passes through the owning task grid, and coords'
        -- prefix is the owning cell.
        grid_path TEXT NOT NULL     -- the checkpoint grid's key_path
            REFERENCES grids(key_path) ON DELETE CASCADE,
        coords TEXT NOT NULL,       -- flattened coords (owner-cell prefix +
                                    -- the checkpoint grid's own axes)
        exec_id INTEGER NOT NULL    -- the owning cell's generation this entry
                                    -- is bound to (replay serves only these)
            REFERENCES task_execs(exec_id) ON DELETE CASCADE,
        step_type_id BLOB NOT NULL, -- the step service's fxtr service type id:
                                    -- the recorded step-fn identity
        args_id BLOB NOT NULL,      -- entity id of the entity-encoded step
                                    -- args (the recorded invocation payload;
                                    -- entity form pins the args' closures)
        value_id BLOB NOT NULL,     -- entity id of the recorded step result
        PRIMARY KEY (grid_path, coords)
    ) STRICT
    """,
    "CREATE INDEX checkpoints_by_exec ON checkpoints(exec_id)",
    """
    CREATE TABLE subject_versions (
        -- Interned observed facts: (location, fingerprint) pairs. Many
        -- floating versions of one location may coexist; the *current* one is
        -- whichever matches the location's live contents (that comparison IS
        -- the direct-staleness check). Pure interning: rows are never
        -- load-bearing to delete. '' sentinels, not NULL (sqlite unique
        -- indexes treat NULLs as distinct).
        version_id INTEGER PRIMARY KEY,   -- PRIVATE surrogate id of one
                                          -- observed fact; the exportable
                                          -- identity is (location, fingerprint)
                                          -- (see 'Public vs private identity')
        grid_path TEXT NOT NULL,          -- location: the observed grid's
                                          -- key_path
        coords TEXT NOT NULL DEFAULT '',  -- location: flattened cell coords
                                          -- (JSON), or '' when the subject is
                                          -- the grid itself (grid_config)
        output TEXT NOT NULL DEFAULT '',  -- location: the observed task-output
                                          -- NAME, when the subject is one named
                                          -- output of a task cell (currency is
                                          -- per-output); '' otherwise
        fingerprint BLOB NOT NULL         -- the observed VALUE: cell/frame/
                                          -- handle-key entity id (cell_value),
                                          -- the written id (wrote_cell), or the
                                          -- config entity id (grid_config)
    ) STRICT
    """,
    # Doubles as the reverse-by-location index via leftmost-prefix scans:
    # e.g. the UI's "who observed this cell" is location → versions (this
    # index) → edges (observations_by_version) → observer cells (task_execs
    # rowid PK). No further index needed.
    """
    CREATE UNIQUE INDEX subject_versions_identity
        ON subject_versions(grid_path, coords, output, fingerprint)
    """,
    """
    CREATE TABLE observations (
        -- The op record / staleness edges: generation observed fact. One
        -- entry per (generation, location) is an app-level invariant (the
        -- in-memory record); this PK only forbids duplicate edges.
        exec_id INTEGER NOT NULL          -- the observer (generation)
            REFERENCES task_execs(exec_id) ON DELETE CASCADE,
        version_id INTEGER NOT NULL       -- the observed fact
            REFERENCES subject_versions(version_id),
        kind INTEGER NOT NULL,            -- the relation (OBS_KIND_* constants:
                                          -- 0 cell_value, 1 wrote_cell,
                                          -- 2 grid_config). One fact is shared
                                          -- by different-kind edges only ACROSS
                                          -- generations (its writer + its
                                          -- readers); within one generation,
                                          -- kind is determined by the subject
                                          -- location (read-xor-write per grid;
                                          -- grid_config subjects live at the
                                          -- distinct coords='' location), so
                                          -- this PK is sound
        PRIMARY KEY (exec_id, version_id)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX observations_by_version ON observations(version_id)",
    """
    CREATE TABLE grid_assertions (
        -- Would-create claims (grounding's `asserted_grid`): the generation
        -- opened this grid THROUGH WRITE AUTHORITY, proposing the config at
        -- version_id — an open that would create the grid in an empty
        -- world, so in a replay this generation re-asserts it (reader or
        -- writer alike; which open physically created it is schedule
        -- noise). Monotone per generation: rows accumulate via INSERT OR
        -- IGNORE across the generation's opens — INCLUDING record hits —
        -- as their own idempotent facts, never mutating observation rows
        -- (the safety pass compares the set; the frozen engine collects
        -- without inserting). Claims expire with the generation (cascade).
        exec_id INTEGER NOT NULL        -- the asserting generation
            REFERENCES task_execs(exec_id) ON DELETE CASCADE,
        version_id INTEGER NOT NULL     -- the asserted (grid, config) fact
            REFERENCES subject_versions(version_id),
        PRIMARY KEY (exec_id, version_id)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX grid_assertions_by_version ON grid_assertions(version_id)",
    """
    CREATE TABLE request_edges (
        -- The demand record (grounding's request facts — see the module
        -- docstring, 'Requests are recorded at commitment'): the
        -- generation REQUESTED this subject, written at record-miss time
        -- before the op goes live. Fingerprint-blind by design; the
        -- resolved value (if any) is the matching observation row.
        exec_id INTEGER NOT NULL        -- the requesting generation
            REFERENCES task_execs(exec_id) ON DELETE CASCADE,
        grid_path TEXT NOT NULL,        -- requested subject: grid key-path
                                        -- (no FK — see docstring)
        coords TEXT NOT NULL,           -- requested cell: flattened coords
        kind INTEGER NOT NULL,          -- REQ_KIND_*: 0 task-output demand
                                        -- (feeds `wanted`), 1 data/handle
                                        -- read
        PRIMARY KEY (exec_id, grid_path, coords)
    ) STRICT, WITHOUT ROWID
    """,
    # The reverse lookup ("who requests this location") serving wanted's
    # reader queries and the marking walk.
    "CREATE INDEX request_edges_by_location ON request_edges(grid_path, coords)",
    """
    CREATE TABLE user_sourcing (
        -- Editable user intent (grounding's user_sources_grid /
        -- user_sources_cell): one revocable record per subject. The
        -- coords='' row pins a grid's canonical config ("I assert this
        -- grid exists like this"; auto-upserted by user creates); a
        -- coords!='' row pins a data/handle cell's value ("I assert this
        -- cell holds this"; auto-upserted by user writes). Upserts replace
        -- the pin. Dangling (pin != live state, or grid gone) is derived
        -- at read time; rows deliberately do NOT reference grids —
        -- intent survives deletion. Grid-before-cell invariant: a
        -- coords!='' row requires the coords='' row for its grid_path
        -- (app-enforced in state_store, adds and revokes alike).
        grid_path TEXT NOT NULL,        -- the subject grid's key_path
        coords TEXT NOT NULL DEFAULT '',-- flattened cell coords (JSON), or
                                        -- '' for the grid-level record
        fingerprint BLOB NOT NULL,      -- the pinned config entity id
                                        -- (grid rows) / value entity id
                                        -- (cell rows)
        PRIMARY KEY (grid_path, coords)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE grid_refs (
        -- Reverse rule-reference index: the task grid at referrer_path's
        -- stored rule addresses referenced_path (TERMINAL slice paths
        -- only — the task_fn slice's grid + each input slice's grid;
        -- enclosing-subtree breakage is covered by grounding's parent
        -- clause, and the rmdir-style delete cannot remove a parent while
        -- the referenced grid exists). Populated at grid create from the
        -- stored rule; rows cascade with the referrer. referenced_path is
        -- a bare path (no FK): rules may outlive their referents, which
        -- is exactly what the index exists to surface.
        referrer_path TEXT NOT NULL     -- the referencing task grid
            REFERENCES grids(key_path) ON DELETE CASCADE,
        referenced_path TEXT NOT NULL,  -- one grid its rule addresses
        PRIMARY KEY (referrer_path, referenced_path)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX grid_refs_by_referenced ON grid_refs(referenced_path)",
    """
    CREATE TABLE wanted_stamps (
        -- The `wanted` memo (grounding's demand predicate), keyed by task
        -- CELL location — deliberately not by generation: wantedness is a
        -- property of the cell (targets + other cells' reader records),
        -- so a cell's own generation turnover must not sweep it, and a
        -- targeted never-run cell needs a stamp home. Row-absence =
        -- unstamped (marking deletes rows); no FK — locations may outlive
        -- grids briefly during deletes, which delete these rows
        -- explicitly.
        grid_path TEXT NOT NULL,    -- the task grid's key_path
        coords TEXT NOT NULL,       -- the cell's flattened coords (JSON)
        verdict TEXT NOT NULL,      -- 'wanted' | 'unwanted'
        reason TEXT,                -- diagnosis; NULL iff wanted
        as_of TEXT NOT NULL,        -- stamp time (ISO)
        PRIMARY KEY (grid_path, coords)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE targets (
        -- Durable declarative intent: wanted DAG roots (model.TargetRecord).
        -- PK over both ids gives add_target idempotence.
        slice_id BLOB NOT NULL,         -- entity id of the entity-encoded
                                        -- wanted slice template (root frame:
                                        -- literals and keeps only)
        expected_config BLOB NOT NULL,  -- entity id of the wanted grid's
                                        -- expected canonical config, captured
                                        -- at marking time (mismatch or absence
                                        -- = the target is dangling)
        grid_path TEXT NOT NULL,        -- denormalized: the wanted grid's
                                        -- key_path (from the slice) — serves
                                        -- the grid-before-cell invariant
                                        -- checks and targets-by-grid queries
                                        -- without decoding slice entities
        coords TEXT NOT NULL,           -- denormalized: the wanted cell's
                                        -- flattened coords (targets are
                                        -- fully bound, so this is definite)
        PRIMARY KEY (slice_id, expected_config)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX targets_by_grid ON targets(grid_path)",
    """
    CREATE TABLE sandbox_bindings (
        -- Durable sandboxed-binding records (notes/sandboxed_bindings.md):
        -- a nix service defn + optional JSON config per handle KEY id.
        -- Keyed by the handle key id like ephemeral bindings — a binding
        -- serves that key wherever it appears, never a cell. NOT identity
        -- (no currency/grounding marks; no handle fingerprint); the runner
        -- owns them. Liveness: key_id (and each dep's pinned_key_id) is a
        -- recorded id, so its closure is pinned in the entity store — bind
        -- persists the bound key's closure, dep keys come from cells (already
        -- persisted).
        key_id BLOB PRIMARY KEY,    -- handle key id-string bytes: the binding
                                    -- identity AND the liveness anchor
        git_commit TEXT NOT NULL,   -- defn triple: the pinned commit
        flake_attr TEXT NOT NULL,   -- defn triple: the flake attribute
        flake_path TEXT,            -- defn triple: optional sub-path
                                    -- (NULL = flake root)
        config_json TEXT            -- optional binding config as CANONICAL
                                    -- JSON text (sorted keys, compact — so
                                    -- export diffs are deterministic); NULL =
                                    -- no config. Plain JSON only (no
                                    -- entities/refs) and NOT identity, so this
                                    -- is the one non-entity blob column
    ) STRICT
    """,
    """
    CREATE TABLE sandbox_binding_deps (
        -- One row per named dependency of a sandboxed binding: a pinned
        -- scalar HANDLE-cell selection. Normalized (own table) so breakage
        -- is reason-able bidirectionally and the chunk-4 TOML projection is
        -- direct. Deps are UNORDERED — keyed by the unique dep_name; a
        -- canonical dep_name sort is used wherever a stable order is needed
        -- (bind error reporting, export, listing). Location is path text with
        -- NO FK to grids (like targets/user_sourcing): a dependency of a
        -- since-deleted grid stays a fact of the binding (surfaced when a
        -- connect re-reads it — chunk 3).
        key_id BLOB NOT NULL        -- the owning binding
            REFERENCES sandbox_bindings(key_id) ON DELETE CASCADE,
        dep_name TEXT NOT NULL,     -- the dependency name the guest receives
        grid_path TEXT NOT NULL,    -- the dep cell's grid key_path (JSON)
        coords TEXT NOT NULL,       -- the dep cell's fully-bound flattened
                                    -- coords (JSON)
        pinned_key_id BLOB NOT NULL,-- the handle key id read from the cell at
                                    -- bind time (the compatibility pin;
                                    -- re-checked at connect — chunk 3)
        PRIMARY KEY (key_id, dep_name)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE events (
        -- Passive append-only OP-LOG (not load-bearing in v0): durable
        -- provenance of "who did what, when"; never links, never a
        -- message stream (task/step output is the runtime-only
        -- run_log.py buffer). Current writers: the importer's 'import'
        -- row.
        -- TODO: record the other mutation/lifecycle op-log events here
        -- (user writes/clears/deletes, target and sourcing edits,
        -- materialization events) as a later round.
        seq INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotone event order
        ts TEXT NOT NULL,                       -- wall-clock timestamp (ISO)
        kind TEXT NOT NULL,                     -- event discriminator
        payload TEXT                            -- JSON details, kind-specific
    ) STRICT
    """,
)


def id_blob(fxtr_id: FxtrID) -> bytes:
    """FxtrID → id-string bytes (the stored BLOB form)."""
    return bytes(fxtr_id.id_string(), "utf-8")


def id_of(blob: bytes) -> FxtrID:
    """id-string bytes → FxtrID."""
    return FxtrID.from_id_string(blob.decode("utf-8"))


def create_workflow_state_schema(conn: sqlite3.Connection) -> None:
    """Create the workflow state-db schema on a fresh database.

    Args:
        conn: An open connection to an empty database. The caller owns
            journal-mode / foreign-key pragmas (the runner uses WAL +
            ``PRAGMA foreign_keys = ON`` on every connection).
    """
    with conn:
        for stmt in SCHEMA_SQL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
