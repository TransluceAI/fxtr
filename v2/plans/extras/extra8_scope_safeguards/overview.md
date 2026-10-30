# Extra 8 — scope-lifetime safeguards

Tooling and affordances that make scope-lifetime deadlocks (a
long-lived scope implicitly adopting an ephemeral ambient scope as a
structural parent, so the ephemeral scope can never drain) diagnosable
in minutes and preventable by construction. The design direction was
settled in the fxtr_experiment review rounds recorded at
`projects/fxtr_experiment/plans/phase6_sandboxing/chunk3post2_scope_safeguards.md`
(the rationale trail, including the debugging case study that
motivated everything:
`projects/fxtr_experiment/plans/phase6_sandboxing/chunk3_scope_map.md`);
these files are the canonical implementation plans.

Each part lands as its own commit, in order:

- [x] [part1_drain_report.md](part1_drain_report.md) — **the
      scope-drain report**: an always-on live-scope registry; a
      manual `dump_scope_report()`; an opt-in signal-receiver CM
      users wrap their main loop in; a `fail_after`-style guard
      (`fail_after_and_report_hung_scopes`) that dumps-then-cancels
      at its deadline; an env-var tier adding both allocation- and
      scope-creation tracebacks. Includes
      the hazard *classification* plumbing: per-parent-edge
      provenance (explicit include vs implicit active-scope capture,
      same-task vs fork-inherited) recorded at scope construction and
      rendered in the report, so a wedged drain names its blocker and
      the hazardous edge directly.
- [x] [part2_docs.md](part2_docs.md) — **the already-true doc
      additions** (pulled forward — everything in them is true once
      part 1 lands): the avoid-capturing-ambient-context-in-tasks rule in
      `v2/notes/scope_discipline.md` and the debugging-tips section
      in `v2/notes/scope_service_lifetimes.md`, both citing the
      part-1 tools.
- [x] [part3_scope_poisoning.md](part3_scope_poisoning.md) —
      **scope-context hygiene affordances**: a hidden poison sentinel
      for `ACTIVE_SCOPE` (`get_active_scope()` under it errors
      pointedly; `no_active_scope()` is the region form) and
      `start_with_scope` / `start_soon_with_scope` task-start helpers
      in `fxtr2/common/task_utils.py` (`scope=None` = poison); adds
      its own one-sentence doc mentions. Core v2 adopts nothing yet;
      the sandbox-launcher migration is a later followup.
