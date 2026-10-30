# Chunk 2 — derived predicates: the LFP evaluator + stamps + marking

**Status: complete.** (182 passed + pyright clean; suites =
`tests/test_grounding.py` — tri-states, dangling pins, status-blindness,
would-start chains, both cycle kinds, stamp lifecycle incl. reopen
durability and the turnover-keeps-wanted-memo property — and
`tests/test_grounding_e2e.py` — a real pipeline all-grounded, the
mint-compat break via reshaped recreate, and the forcing-argument
parent↔child shape asserted fact by fact.) Two evaluator bugs were found
by the test round and fixed in the same landing: stamp writes lacked the
committing transaction wrapper (the two-phase split had dropped
currency's ``with conn:``), and stamped rule-reference deps' config ids
were not recorded for referrers' mint checks (an order-dependent false
``unanchored``); both are pinned by regression tests.

## Scoping-round resolutions (2026-07-06)

- **Faithfulness bar**: the implementation matches the datalog of
  `notes/grounding.md` exactly; any surprise forcing a semantic deviation
  pauses the chunk for a re-plan rather than being absorbed silently.
- **The correctness argument is a deliverable**: the module docstring
  (plus per-function notes) must let a reader who knows the design but
  not the code verify (a) the invariants always true of the DB/Python
  state, (b) what they imply about the state ↔ datalog-predicate
  relationship, (c) at each mutating function, why the invariants are
  upheld, and (d) at each derived-property query, why it computes the
  datalog's answer, by appeal to the invariants.
- **Bidirectional cone, currency-hybrid evaluation**: live stamp serves
  O(1); otherwise load the fact's justification component (observations
  both directions, `grid_refs` + parent edges, targets/sourcing
  attributes) in one transaction, run the LFP in Python, stamp
  everything derived. Component loading may use a Python frontier loop
  inside one conn body (the heterogeneous node kinds make a single CTE
  awkward); over-loading is safe, under-loading is not.
- **Running generations never stamp their own verdicts** (currency's
  rule carried over); facts depending on a running generation's edges
  may stamp because every edge insert marks its neighbors in-txn.
- **The I2-free soundness argument** (no reliance on the strict gate):
  evaluation stamps the entire derived cone; every mutating body marks
  its immediate justification-graph neighbors (both directions);
  propagation prunes at unstamped-or-running — sound because a stamped
  fact's cone can only gain an unstamped member through a mutation whose
  mark cleared the stamped fact first. Written out in the module
  docstring; verified per trigger during implementation.
- **The self-vouching seam moved to chunk 3** (deviation from the
  scoping round's "from day one"): a pending-edge parameter without its
  consumer would land untested; the two-phase session entry point
  (`WorkflowSession._grounding`) is where it threads, and nothing in the
  evaluator's shape blocks it — the gate chunk adds it together with its
  tests.

`grounding.py` as a store-internal sibling of `currency.py`, implementing
the joint least fixed point of `notes/grounding.md` (`wanted` /
`would_start` / `grounded_grid` / `grounded_data_cell` /
`grounded_task_cell`) with stamped verdicts.

## Scope

- **Evaluator**: one atomic body per query — serve a live stamp O(1), or
  bulk-load the relevant cone (recursive CTE; note the cone spans *both*
  observation directions — wantedness walks readers, groundedness walks
  inputs — plus grid-dep edges), run the LFP in Python, stamp settled
  verdicts back. Running generations never stamp (their records are
  moving targets), same rule as currency.
- **Verdict shape**: tri-state per fact — ``grounded | unanchored |
  orphaned`` — plus reason + as_of. Orphaned (the depth-1 no-claimant
  fact) folds into the same stamp because its marking triggers coincide
  with the transitive verdicts'; the gate's local-only level then reads
  one stamp. Document explicitly that *unanchored is not predictive* the
  way currency's unsettled is (no "resolves when X settles" semantics).
- **Stamp placement**: `grounded_*` columns on `grids`, `cells`, and
  `task_execs` (each verdict's lifecycle coincides with its row's) — but
  **`wanted` lives in a location-keyed memo table**
  (`wanted_stamps(grid_path, coords, ...)`, row-absence = unstamped,
  marking deletes rows): wantedness is a property of the *cell*, not the
  generation — a cell's own turnover must not sweep it, and
  generation-less targeted cells need a stamp home.
- **Marking** (the trigger table in `notes/grounding.md`), wired into
  `state_store.py`'s mutating bodies under the existing "Marks:"
  docstring convention: sourcing add/revoke, target add/remove,
  observation insert (both directions: `read_out` marks the subject's
  wantedness; `wrote`/asserted-config mark the subject's groundedness),
  generation turnover (everything the departing record claimed *or*
  wanted), grid create/delete (dependents via the reverse rule-reference
  index, plus contents).
- **Soundness without the strict gate**: the prune-at-unstamped walk may
  not assume "no new edge attaches to an ungrounded subject" (that
  invariant holds only while the gate knob is strict) — use the
  conservative edge-insert marking rule and record the argument in the
  module docstring.

## Tests

- Verdict scenarios: the sub-workflow derivation (the forcing argument's
  E↔child shape comes out grounded), adoption, rule-reference and demand
  cycles (not grounded, diagnosed), orphan flips on turnover and
  revocation, unanchored-vs-orphaned distinction, handle cells
  (user-sourcing only).
- Stamp lifecycle mirroring the currency suite: marking precision,
  re-stamp on re-verify, reopen durability, the wanted-memo lifecycle
  under turnover (stamp survives the cell's own turnover; flips on a
  *reader's* turnover).

Notes/log: (fill in during the chunk)
