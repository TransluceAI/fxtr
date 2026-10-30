# Extra 8 part 1 — the scope-drain report

**Status: IMPLEMENTED** (`fxtr2/debug/`; tests
`v2/tests/test_scope_report.py`; the e2e-guard conversion is the
follow-up commit noted in the steps). Decisions confirmed as the
strawmen below. Design direction and rationale trail:
`projects/fxtr_experiment/plans/phase6_sandboxing/chunk3post2_scope_safeguards.md`
(items 1–2); the motivating case study:
`projects/fxtr_experiment/plans/phase6_sandboxing/chunk3_scope_map.md`.
A working prototype of the introspection half (scope registry +
outstanding-lease dump) lives in the abandoned `fxtr expt 6.3 debug
residue` jj commit (`tests/_debug_taskdump.py`) — rewrite, informed by
it.

## What it is

One diagnostic: for a hung scope drain, print *why* — every wedged
drain, the outstanding leases blocking it (with human-readable holder
labels), each blocker scope's parent edges classified (explicit
`include=` vs implicit active-scope capture; same-task vs
fork-inherited), and a pointed hazard line when a blocker holds a
wedged scope via an implicit cross-task edge. Always one global,
traversing report (per-scope dumps miss the diagnosis: the
explanatory node is often an OPEN scope that nothing would trigger
on).

Worked example of the target output: the chunk3post2 file's
"Worked example" section.

## Triggers (settled shape)

- **Manual**: `dump_scope_report()` — synchronous, callable anywhere
  (including signal handlers and pdb).
- **The guard**: `fail_after_and_report_hung_scopes(seconds, *,
  report=True)` — a `fail_after`-style async CM that, at its
  deadline, first emits the report (the wedge is still pristine —
  nothing has unwound), then cancels. Tests opt in by using it;
  `report=False` / plain `anyio.fail_after` opt out. Implementation:
  a timer task (anyio's deadline cancellation has no pre-hook), so
  semantic deltas vs `anyio.fail_after` (checkpoint timing,
  shielding) must be documented on it.
- **Signals, opt-in, NEVER globally installed**: an async context
  manager (working name `report_hung_scopes_on_signal(*signals)`,
  default `SIGUSR1`) built on `anyio.open_signal_receiver` — it runs
  a small receiver task that calls `dump_scope_report()` per
  delivery. Users who want live-process dumps wrap their main loop
  in it themselves; fxtr2 installs nothing at import or module
  level. (A wedged live process is observable *now* — no
  cancellation needed.) Do NOT accept `SIGALRM` (the test conftest
  owns it — `v2/tests/conftest.py:64`); fxtr2 currently installs no
  signal handlers anywhere, so `SIGUSR1`/`SIGTERM` are uncontested.
- **Tracebacks**: `FXTR_SCOPE_REPORT_TB=1` — an env-var front-end
  over the *existing* `local_lease.LEASE_LEAK_TRACK_ALLOCATIONS`
  module flag (which nothing reads from the environment today; tests
  flip it programmatically): at startup it sets that flag AND
  enables the net-new scope-creation-traceback capture, so every
  lease line carries who-took-this and every scope carries
  who-created-this. One switch because a rerun always wants both.
- **No armed hook** (a one-shot trigger on cancelled-while-parked
  drains, for give-ups fxtr2 cannot see coming) — DROPPED as
  more machinery than the coverage justifies: the guard covers
  tests/harnesses, the signal CM covers live processes, and the
  manual dump covers everything else. Dropping it also deletes the
  arming state machine, the parked-duration-floor tuning, and the
  only hook required inside the drain-cancellation path (drain
  *timestamps* stay — the report still shows parked durations).
  Revisit only if a real case shows up that neither remaining
  trigger reaches (recorded in Deferred below).
- **No timer watchdog** (report-after-N-seconds-parked) — dropped;
  the signal CM covers its use cases.

Emission is strictly synchronous (no awaits — it may run in a signal
receiver or at a cancellation boundary), to stderr via `print` like
the lease-leak warnings.

## Instrumentation inventory (the net-new state, all cheap-always)

Verified integration points as of this writing:

1. **Live-scope registry** (net-new; none exists): a module-level
   `weakref.WeakSet[FxtrScope]`, populated at scope entry
   (`FxtrScope._do_open`, `entity_ref_scope.py` ~1505). Registry +
   report builder live in the new module (below); at dump time it
   also provides the reverse map (derived lifetime → scope) used to
   resolve lease holders to scope names.
2. **Activation-task record**: a parallel contextvar recording
   `(scope, current_task())` at the exact two primitive
   `ACTIVE_SCOPE.set` sites — non-isolated CM entry
   (`entity_ref_scope.py:2764`) and `as_active_scope()` (`:2838`);
   every other activation funnels through the latter (the dispatch
   layer activates via `call_scope.as_active_scope()`,
   `service_cap_common.py:1661`). Contextvar set/reset cost is
   nanoseconds; `as_active_scope` is hot, so this gets a perf
   sanity check.
3. **Per-edge provenance on `FxtrScope`**: the implicit parent is
   *already* stored separately (`_outer_scope`,
   `entity_ref_scope.py:1292–1296`; `include=` parents are
   `_parents` minus it), so classification needs only two new
   fields at `_init_common`: `_implicit_capture_cross_task`
   (compare the activation record's task against `current_task()`
   at capture) and `_creation_site` (caller `file:line` via one
   `sys._getframe(1)` peek — **always-on**: a sub-µs cost per
   construction that makes the report actionable without a rerun;
   note the existing name-inference frame walk only runs when
   `name=None`, so this is a genuinely new per-construction cost on
   the dispatch hot path (two scopes per call) — covered by the
   perf-sanity step, degrade to TB-flag-gated only if it actually
   measures).
4. **Dispatch call-scope flag**: the per-call scopes are created at
   `service_cap_common.py:1394–1418` with only a `/call/<method>`
   *name* convention today; add an explicit boolean
   (`_is_dispatch_call_scope`) at those two creation sites so the
   report can label them `[dispatch per-call scope]` without name
   parsing.
5. **Lease holder labels** (net-new; no label field exists on
   leases/grants): add `holder_label: str | None` to the frozen
   `_LeaseCleanupInfo` (`local_lease.py:84`; populated in the two
   `_register_lease` impls, `:565` / `:1053`), threaded from the
   mint paths. Label at least the independent-lease bridges
   (`FxtrScope._build_independent_lease_bridge`,
   `entity_ref_scope.py:4757` → `_mint_lease_via_ancestor_bridge`)
   as `"independent_lease into <dest scope name>"`. Structural
   parent-via leases need no label — their grant's derived lifetime
   resolves to the child scope via the registry reverse map.
6. **Drain timestamps**: "draining" is derivable today
   (`is_open()` and `_scope_auth_lease is None`) but has no
   timestamp; record drain-start when `FxtrScope.aclose`
   (`entity_ref_scope.py:2458`) reaches its wait
   (`await self._derived.aclose()` at `:2506`; the real wait is
   `DerivedLocalLifetime.aclose`'s `end_event.wait()`,
   `local_lease.py:1000–1020`). The second wait
   (`_lifespans_closed_event.wait()` at `:2532`) gets its own
   timestamp. Pure records — with the armed hook dropped, nothing
   hooks the cancellation path.

## The report builder

- **Wedged set**: registered scopes currently draining
  (drain-start timestamp set, token not ended), sorted by parked
  duration — for the guard trigger that means "parked at deadline";
  for manual/signal dumps, "parked now".
- **Per wedged scope**: name, creation site, `[dispatch per-call
  scope]` tag, parked duration; its outstanding output leases
  (`_derived._output_tracking` → `lease_repr`, `holder_label`,
  `alloc_tb` when tracked); each lease's holder resolved to a scope
  where possible (label, or derived-lifetime reverse lookup), with
  that holder's status (OPEN / draining / wedged-see-above) and its
  parent-edge classification.
- **The hazard line**: printed when a wedged drain's blocker holds it
  via an edge that is implicit AND fork-inherited — the general
  hazard class (deliberately not dispatch-specific; dispatch scopes
  are just the most prolific ephemeral-ambient producers). Points at
  `v2/notes/scope_discipline.md`.
- **Appendix**: the full live-scope inventory (name, age, draining?,
  parent edges, lease count), then a footer naming the TB rerun flag.

## Decisions to confirm at implementation start

- Module home: proposal `v2/fxtr2/debug/scope_report.py` (new small
  package; nothing similar exists today) — the instrumented modules
  import it lazily/locally to avoid cycles.
- Whether the guard skips the report when nothing is parked at its
  deadline (strawman: yes — a timeout with no parked drains is a
  slow computation, not a wedge; print one line saying so).
- The signal CM's exact name and default signal set.

## Implementation finding

- **The portable wheel enumerates subpackages explicitly**
  (`v2/portable/pyproject.toml` force-include): the core's
  module-level import of `fxtr2.debug.scope_instrumentation` made
  `fxtr2/debug` a *required* entry there — without it, wasm/pyodide
  guests (which install fxtr2-portable) cannot import
  `entity_ref_scope` at all. Caught by the fxtr_experiment wasm e2e;
  any future fxtr2 subpackage the core imports needs the same entry.

## Deferred

- **The armed hook** (one-shot report on a drain cancelled while
  parked, for give-ups fxtr2 cannot see coming — a plain
  `anyio.fail_after`, an outer harness, Ctrl-C): dropped from v0 as
  more machinery than the coverage justifies. Bring back only on a
  demonstrated need; it requires the arming state machine, a
  parked-duration floor, and a hook in the drain-cancellation path.

## Steps

- [x] The debug module: registry, report builder, renderer,
      `dump_scope_report()`, the `FXTR_SCOPE_REPORT_TB` startup
      check, the `report_hung_scopes_on_signal` CM.
- [x] Instrumentation: activation-task record; per-edge provenance +
      creation site; dispatch call-scope flag; drain timestamps;
      holder labels on the independent-lease mint path;
      scope-creation-traceback capture behind the TB flag.
- [x] `fail_after_and_report_hung_scopes` (+ its semantic-deltas
      docstring).
- [x] Tests (`v2/tests/`): registry lifecycle; edge classification
      (include vs implicit, same-task vs forked); holder labels; a
      synthetic wedge fixture (the residue commit's minimal-pair
      shape, built to *unwedge* cleanly after dumping) asserting the
      report names the blocker, the edge class, and the hazard line;
      guard dump-then-cancel + `report=False`; the signal CM
      (deliver SIGUSR1 to self, assert a report); TB flag adds both
      traceback kinds; no-parked-drains timeout prints the
      one-liner.
- [x] Perf sanity (measured): activation record set+reset 74ns per
      dispatch, creation-site peek 141ns per construction, registry
      add 81ns per entry — each <7% of bare FxtrScope construction
      (2.1us), which scope *entry* dwarfs. Always-on confirmed.
- [x] Update `v2/CODE_OVERVIEW.md` (new debug package + the
      instrumentation's one-line mentions) and check off here +
      in `overview.md`.
- [ ] **Follow-up commit, immediately after this one**: convert the
      fxtr_experiment / fxtr_sandboxing e2e `anyio.fail_after`
      guards (and the launcher-scopes regression test) to
      `fail_after_and_report_hung_scopes`.
