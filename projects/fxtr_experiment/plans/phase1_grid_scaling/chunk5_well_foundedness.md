# Chunk 5 — grid-level well-foundedness

**Status: absorbed into the grounding design.** The scoping round happened
as part of the 2026-07-06 grounding rounds (logged in
`notes/discussion_log.md`); the canonical writeup is `notes/grounding.md`.
Grid well-foundedness is now the dependency clause of `grounded_grid`
(every `grid_dep` reference grounded + mint-compatible), one fragment of the
intent-axis least fixed point, and the open questions below all resolved
there:

- *Existence cycles*: not derivable in the LFP → not grounded; no special
  case (mirrors currency's treatment of observation cycles).
- *Recompute-on-recreate*: grounding verdicts are stamped derived state;
  create/delete mark dependents via the reverse rule-reference index and
  the next query re-derives (currency-stamp pattern).
- *Gate placement / classification*: the structural mint gate, pre-run,
  fault-class → interrupted.
- *Config compatibility*: folded in as `mint_compatible` in the dependency
  clause.
- *Surfacing*: grounding verdicts + diagnosis chains in the introspection
  results; `delete_grid` consequences reported at delete time
  (refuse-vs-notify `force` toggle deferred to the UX round).

The `delete_grid` deferred item in `notes/design_overview.md` is resolved
accordingly; implementation is planned with the rest of the grounding
work (phase-2), not as a phase-1 chunk.

## Original scope (phase-scoping round — superseded)

Make "can this grid's cells ever run fresh?" a stored, queryable,
grid-level fact:

- **Well-founded(grid)** = every grid its stored rule references (inputs,
  the task-fn slice's grid, enclosing subtrees) exists — transitively
  through those grids' own rules.
- **Forward-looking only**: never touches the currency of completed
  generations (holding ≠ observing unchanged); `delete_grid` stays
  allowed; demand *consults* the flag for a faster, richer diagnosis
  while grant-mint failure remains the enforcement.
- Maintained via a reverse rule-reference index + flag propagation
  (chunk 4's marking discipline), surfaced in `KeyInfo` /
  `inspect_cell` / `list_targets`.

## The 2026-07-06 discussion: why that scope didn't survive contact

1. **The transitive predicate, as originally stated, doesn't correspond
   to a crisp operational claim.** "Every referenced grid exists,
   transitively" is *not* "a fresh run of g can succeed": if g's input
   grid exists but that grid's own input was deleted, a fresh run of g
   still succeeds whenever the input *cells* it demands are complete and
   fresh — completed generations keep serving (holding ≠ observing). So
   the stored transitive flag would predict neither mint success (that's
   direct existence) nor run success (that's currency's job).
2. **Direct existence alone is honest but thin.** It is exactly "grant
   mint can succeed", trivially maintainable (no propagation, no cycle
   questions — delete flips exactly the grids whose rules name the
   deleted path). But then a stored flag + reverse index buy little over
   *computing* it at read time, and a purely dynamic version reduces to
   a slightly improved diagnostic — it doesn't actually resolve the
   `delete_grid` deferred item (deletion still gives no warning; nothing
   structural is stated). Considered and rejected as not worth a chunk.
3. **The direction that is worth doing: increase strictness so the
   transitive predicate becomes true by fiat.** Fresh runs *fail
   immediately* when they depend on any input grid that is not
   well-founded — upgrading "will probably fail at runtime due to
   staleness somewhere upstream" into "fails immediately with a
   structural error". This is only a slight strictness increase (in the
   broken-reference situation the affected cells are typically stale
   anyway), and it buys: transitive well-foundedness that literally
   means "a run of g can succeed (structurally)", clearer and stronger
   system invariants, and obviously-correct diagnostics/introspection.
   At that point storing transitive state (chunk-4-style marking over a
   reverse rule-reference index) earns its keep. This is roughly the
   original scope, plus the run gate, minus the "no behavior change"
   framing.

## Open questions for the scoping round

- **Existence cycles.** Rule references can be made cyclic via delete +
  recreate-with-a-back-reference (creation-time existence checks prevent
  cycles only on the first construction). What does the transitive
  predicate say about an all-exists cycle — well-founded (existence has
  no order dependence) with currency left to park it, or rejected?
- **Recompute-on-recreate.** Deletion propagates not-well-founded up the
  reverse index (prune-at-already-broken, like stamp-clearing); but
  *re-creating* a grid must recompute dependents' flags from their
  references' current state — a different, forward-checking walk. Shape
  and cost.
- **Where the strict gate lives and what it raises.** Demand-time
  vs grant-mint-time; presumably a structural fault classified like
  other not-ready conditions (interrupted, not errored) — confirm
  against the error taxonomy.
- **Interaction with currency.** Does non-well-foundedness of a grid
  affect the *currency* of its (settled) cells at all (it shouldn't —
  holding ≠ observing), and does the demand gate consult the flag before
  or after the currency verdict?
- **Predicate refinement: config compatibility.** Existence-only vs
  "exists with a mint-compatible config" (mint reconstructs specs from
  live configs and faults on mismatch today — a recreated grid with a
  different config passes existence but still cannot run).
- **Surfacing.** `KeyInfo` flag; `list_targets` marking targets on
  non-well-founded grids (distinct from dangling); the diagnosis chain
  ("g's rule references deleted grid Y, via X") — derived at read time
  by walking, or stored.

Notes/log: resolved by the 2026-07-06 grounding rounds — see
`notes/discussion_log.md` (the grounding entry) and `notes/grounding.md`
(the canonical writeup, including the answers summarized in the status
block above).
