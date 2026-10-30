# Phase 2 — grounding: the intent axis in the DB and on the surface

Goal: implement the grounding design (`notes/grounding.md` — settled in
the 2026-07-06 design rounds): the editable root facts (revocable
sourcing records + targets), the derived **wanted** / **would_start** /
**grounded_\*** least fixed point with stamped verdicts, the gate policy
seam (strict default), and the user surface for maintaining intent —
ending with the linter and its test suites. Import/export is *not* this
phase; grounding supplies its vocabulary (exported facts arrive labeled),
not its procedure.

## Chunks

Each chunk lands with tests, a CODE_OVERVIEW update, and design-doc
updates as decisions become current state; details and deviations live in
the chunk's own file (promote anything design-relevant to
`notes/discussion_log.md` and the canonical docs).

- [x] [chunk0_open_modes.md](chunk0_open_modes.md) — design round with a
      decision gate: how open modes should work (including the
      auto-infer-most-capable direction), which invariants must survive,
      and the record/hit-matching rule for the *would-create* bit chunk 1
      records. **Outcome: the simplification landed** — opens follow the
      chain, `read_only()` is a holder-local privilege drop feeding the
      wiring, the user `open_handle` keeps an explicit `create` flag,
      and would-create is monotone-OR per generation.
- [x] [chunk1_base_facts.md](chunk1_base_facts.md) — base facts in the
      schema + session ops: the sourcing-records table (maintained
      grid-before-cell invariant), the `grid_assertions` claim table
      (would-create, monotone per generation, safety-pass-compared), the
      reverse rule-reference index, `responsibility` dropped,
      auto-upsert on user writes/creates. No derived state, no gates.
- [x] [chunk2_derived_predicates.md](chunk2_derived_predicates.md) —
      `grounding.py` (sibling of `currency.py`): the joint LFP evaluator,
      tri-state verdict stamps (with the location-keyed `wanted` memo
      table), and the marking discipline in `state_store.py`. **Landed**,
      with the module-docstring correctness argument as a co-deliverable
      and mint-compat implemented via minting's own structural checks.
- [x] [chunk4_user_surface.md](chunk4_user_surface.md) — **runs before
      chunk 3** (order swapped in the 2026-07-07 round: land the
      vocabulary — sourcing CRUD, adoption, verdict surfacing, the
      demand auto-target default — before the behavior-changing gates,
      so the gate chunk's test updates can speak it): sourcing CRUD on
      the user capability, verdict surfacing in the introspection
      results, `delete_grid` consequence reporting, demand auto-target
      (wrapper default; capability stays mutation-free). **Landed**,
      including the demand-requires-target rule (capability-enforced,
      `NotTargetedError`) and the `adopt=` kwarg on writes /
      `add_target` / the demand wrapper.
- [x] [chunk3_gates.md](chunk3_gates.md) — the gate policy seam
      (serve-or-fail: one session op evaluates both axes and serves from
      the same snapshot), self-vouching requests, fault records +
      diagnosis chains, the strict / off knob (local-only dropped), and
      the structural mint gate's immediate diagnosis. **Landed** in the
      request-record form (schema v6 ``request_edges``, written at
      request commitment — the durable-self-vouching design settled in
      the chunk's 2026-07-09 round), with the safety pass extended to
      compare request edges (every durable per-generation fact is
      compared at completion) and user data/handle reads wired through
      the same gate.
- [x] [chunk5_lint_and_suites.md](chunk5_lint_and_suites.md) — the lint
      affordance (the clean-state inventory) + per-removal-kind linter
      unit tests + targeted replay-behavior scenarios. **Landed in the
      re-scoped structural-only form** (the 2026-07-09 round: grounding
      + dangling intent, staleness deliberately out; unmaterialized
      targets are pending intent, not findings; the write-grant
      tightening hint dropped; the adoption scenario folded into the
      unit tests): `lint.py` + `WorkflowSession.lint` + the
      capability's `lint` → `LintReport` with the sound/clean levels,
      the seven unit tests, and the two lint-arc scenarios.

## Cross-cutting decisions (settled in the phase-scoping round)

- **`notes/grounding.md` stays true-now throughout**: every landing chunk
  updates its implementation-status paragraph, and the schema/API
  implications listed there convert to pointers at the real code as they
  land. (The doc was written before any implementation; by phase end its
  status paragraph should say "implemented", with this phase dir as the
  trail.)
- **The `responsibility` table is dropped** (chunk 1): task-side claims
  are derivable from the op record once would-create is recorded
  (`wrote_cell` edges carry the written fingerprint; `grid_config` edges
  + would-create give assertions), with the generation-cascade lifecycle
  inherited identically; user rows are replaced by the sourcing-records
  table (which they had to be — they need revocation). Grep-confirmed:
  the table has writers and docstring mentions only, no readers.
- **Scenario tests, not property tests** (carried forward from phase 1).
  In particular the "lint-clean ⟹ replay reproduces" invariant is *not*
  tested as a delete-everything-derived sweep over a fixed scenario
  (redundant with direct state assertions, poor diagnosability) — it is
  covered by chunk 5's decomposition into per-removal-kind linter unit
  tests plus targeted replay-behavior scenarios. A randomized replay
  property harness is possible future work, deliberately not built now.
- **Stamps follow the currency-stamp pattern** (marks in the mutating
  transaction, verdicts derived on demand, stamps as the only memo), with
  one standing constraint from the design: maintenance must be sound
  *without* the strict gate's no-edges-onto-ungrounded invariant (the
  gate is a knob) — no optimization may quietly assume strictness.
- **No migrations**: pre-release; existing state DBs are recreated.
- **UX-round items stay out of the phase** (deferred in
  `notes/grounding.md`): the final gate-default call, the `delete_grid`
  refuse-vs-notify `force` toggle, the auto-target sugar's final shape
  (chunk 4 implements a v0 shape), and the pruning affordance.

## Status

**The phase is complete**: chunks 0 (the opens-follow-the-chain
refactor), 1 (base facts), 2 (the derived predicates + stamps +
marking), 4 (the user surface — ordered before 3), 3 (the gate policy
seam, in the request-record form), and 5 (the structural linter + its
suites). Phase 1 (`../phase1_grid_scaling/`) is complete; its chunk 5
was absorbed into the grounding design this phase implements.
