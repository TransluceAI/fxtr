# Chunk 3 post 2 — scope-lifetime safeguards (proposal; direction settled, not yet scoped to commits)

**Status: COMPLETE — this file is the rationale trail.** Everything
here landed via `v2/plans/extras/extra8_scope_safeguards/` (part 1 =
items 1–2, the drain report + classification, with the e2e guards
converted; part 2 = items 4–5, the doc additions; part 3 = item 3,
poisoning + the task-start helpers) plus the sandbox launcher's
migration onto `start_with_scope(scope=None)` as its own commit.
Direction was settled over two review rounds (2026-07-21) from the
safeguard candidates distilled out of the deadlock hunt
(`chunk3_scope_map.md`); the root fix
(`chunk3post1_sandboxing_scope_fix.md`) landed separately. The one
deliberately-deferred idea (the "armed hook" trigger) is recorded in
the extra8 part-1 plan's Deferred section.

What made the hunt slow, in one line: the deadlock's *structure* (a
long-lived scope holding an ephemeral per-call scope as parent) was
invisible — every task idle, every wire quiescent — until lease
introspection was hand-built on day N; and the repro harnesses misled
twice because ambient scope context forks into every started task,
including the harnesses' own.

## 1. Explain-drain: the scope-drain report (fxtr2)

The diagnosis tool. Promotes the *capability* behind the residue
commit's `tests/_debug_taskdump.py` (scope registry + outstanding-
lease dump over `LEASE_LEAK_TRACK_ALLOCATIONS`), redesigned rather
than transplanted.

**Trigger design** (settled; round 2 replaced the earlier
threshold-based "report on cancelled long drain" with the
snapshot/arm design below):

The framing insight: duration alone can never be a clean signal —
designed permanent drains exist (`_serve_spawn_rpc` lives in
`__aexit__` for the process's life; dispatch call scopes park while
callers hold returned refs) — the ground truth is *someone giving
up*. And when the giver-upper is fxtr2-aware, it needs no detection
at all: **at its deadline the wedge is still fully intact, so it
dumps first and cancels second.** The report is then a pristine
snapshot (every parked drain with its true parked duration, nothing
mid-unwind), and the wedged-vs-passing-through classification the
threshold was approximating becomes exact — a timeout that fired
while real work was still running shows no parked drains, and the
report is skipped.

- **Layer 0 — manual, always available.** A module-level
  `dump_scope_report()` over a live-scope registry. The registry is
  the one always-on piece: a `WeakSet.add` at scope entry,
  effectively free, so manual dumps need no pre-configuration.
  Opt-in signal handlers reuse it directly: `SIGUSR1` →
  synchronous dump (a wedged live process is observable *now* — no
  reason to wait for a cancellation), and optionally dump-on-SIGTERM
  for unattended contexts with external kill timeouts.
- **Layer 1 — the guard wrapper.** A `fail_after`-compatible CM
  (fxtr2-provided) that on deadline dumps, then cancels. This is how
  tests opt in — the e2e `fail_after` guards convert to it, and a
  hang that today prints a bare `TimeoutError` prints the diagnosis
  instead. Per-test opt-out is trivial (use plain
  `anyio.fail_after`, or a `report=False` arg) for tests that
  genuinely exercise deadlock behavior — so there is no ambient
  pytest default to gate. Implementation note: anyio's deadline
  cancellation happens inside its cancel-scope machinery with no
  pre-hook, so the wrapper is a timer task that dumps-then-cancels;
  its semantics vs real `fail_after` (checkpoint timing, shielding)
  need care.
- **Layer 2 — the armed hook**, for give-ups fxtr2 cannot see coming
  (a plain `anyio.fail_after`, an outer harness, Ctrl-C):
  `arm_scope_report()` — sync, callable from anywhere — or
  `FXTR_SCOPE_REPORT=armed` at process start puts a **one-shot** hook
  on cancelled drains; the first cancelled parked drain fires the
  global report and disarms; re-arm for another. Rate limiting *is*
  the arming state machine — no time windows. This path fires
  mid-unwind, so it is slightly racier than layer 1 and may want a
  tiny parked-duration floor to skip healthy drains the cancellation
  merely swept through; since the report is global, trigger
  precision barely matters (the header just names whichever drain
  tripped it).
- **Layer 3 — env-var opt-in tracebacks.** Any of the above, with
  `LEASE_LEAK_TRACK_ALLOCATIONS` (+ scope-creation tracebacks)
  pre-enabled so every lease line carries who-took-this. Necessarily
  a rerun flag — tracebacks cannot be captured retroactively.
- **Demoted to a maybe: the timer watchdog** (dump after N seconds
  of parked drain, no cancellation). The signal handlers cover the
  wedged-live-process and external-kill cases it was for.

**One traversing report, not per-scope spam** (settled): the report
is always a single global snapshot (layer 1 emits exactly one per
deadline; layer 2's arming state machine makes it one per arming by
construction). This is not just dedup: per-scope dumps would
genuinely *miss the diagnosis*. Wedged drains are what you notice,
but the explanatory node may not be draining at all — in the chunk-3
hang, `sandbox_env` was parked OPEN (waiting on a teardown that could
never come), and the hazardous edge classification lives on it, the
*child*. The report therefore traverses: for each wedged drain,
follow its outstanding leases to the holders and print the holders'
edge classifications — "here is why", not "here are N stuck scopes".

**Worked example** — the chunk-3 (e) hang through the proposed report
(layer-1 header shown; the armed hook's header names the cancelled
drain that tripped it instead):

```
======== fxtr2 scope-drain report ========
trigger: guard deadline (30s) reached; 3 drains parked

Wedged drains:

1. 'call:SpawnGateImpl.get#17'  [dispatch per-call scope]  parked 30.0s
   created: service_cap_common.py:1391 (task 'dispatch:SpawnGateImpl.get')
   blocked on 1 outstanding lease of its main token:
     - parent-via lease held by scope 'sandbox_env' (OPEN, not draining —
       will not end before this scope can)
       edge: IMPLICIT active-scope capture, CROSS-TASK
         ACTIVE_SCOPE set in task 'dispatch:SpawnGateImpl.get'
         captured in task 'FxtrSandboxLauncher._env_holder' at spawner.py:899
   !! hazard: long-lived scope implicitly derives from an ephemeral
      per-call scope (see scope_discipline.md)

2. 'wf3-sandbox:fxtr.services.binding_echo'  parked 29.8s
   created: sandbox_launcher.py:254 (task 'FxtrSandboxLauncher._launch_holder')
   blocked on 1 outstanding lease:
     - bridge lease 'independent_lease into call:SpawnGateImpl.get#17'
       (holder wedged: see 1)

Live-scope inventory (11 scopes): [name, age, draining?, parent edges,
lease count] …
======== end report (rerun with FXTR_SCOPE_REPORT_TB=1 for allocation tracebacks) ========
```

The load-bearing line is the classified edge with *both* locations —
where `ACTIVE_SCOPE` was set and where it was captured.

**Cheap-always vs gated info**: scope name, creation site (one caller
frame — `FxtrScope` already walks it for name inference), per-edge
classification (two booleans at construction; item 2), drain-start
timestamps, task names (`current_task()`), and structural parent-via
holder identity are all effectively free and always recorded.
Allocation tracebacks are layer 3. One plumbing requirement: **bridge
leases need a holder label minted at creation** ("independent_lease
into X") so the traversal can follow them without tracebacks.

**Strawman names** (react at implementation planning): module
`fxtr2.debug` (or beside `fxtr2.leases`); `dump_scope_report()`,
`arm_scope_report()`, the guard wrapper `fail_after_report(seconds,
*, report=True)`; env vars `FXTR_SCOPE_REPORT=armed` /
`FXTR_SCOPE_REPORT=sigusr1` (combinable), `FXTR_SCOPE_REPORT_TB=1`.

**Settled: rewrite, informed by the residue.** The residue commit's
`tests/_debug_taskdump.py` is the seed (it proves the info suffices)
but not the implementation: the always-on entry-time registry
replaces the `__init__` monkeypatch, holder labels replace
repr-archaeology, and the report layer is new.

**Remaining open questions** (settle at implementation planning):
exact API/flag names (above); the guard wrapper's semantic deltas vs
`anyio.fail_after` (timer-task implementation — checkpoint timing,
shielding); whether the armed hook needs the tiny parked-duration
floor; report emission must be synchronous (no awaits — it may run
mid-cancellation or in a signal handler); whether the demoted timer
watchdog is worth keeping at all.

## 2. Hazard classification (rides item 1; formerly "detection")

**Settled: the hazard class is general, not dispatch-specific.** The
mechanism is: a scope implicitly captures an ambient parent whose
lifetime is shorter than its own, via context forked into a task that
outlives that parent. Dispatch call scopes are merely the most
prolific producers of ephemeral ambient scopes (and the sneakiest —
the framework activates them invisibly); a user's per-request
`async with FxtrScope():` around a `start_soon` of a long-lived
worker reproduces it with no dispatch anywhere.

**Settled: no construction-time warning as the primary mechanism.**
The wrong thing is a lifetime relationship ("S outlives P's intended
end"), which is not observable at construction; the observable
discriminators over-trigger — cross-task capture is routine and safe
when the task lives inside the captured scope's body (workers on a tg
enclosed by the scope's CM), *even for dispatch scopes* (a tg inside
a dispatch method whose workers open non-isolated scopes is fine).

Instead, **record cheap provenance at non-isolated construction and
let the drain report classify blockers**: per parent edge, whether it
was explicit (`include=`) or implicit (active-scope capture), and
whether the capture was same-task or fork-inherited. Implementable
with a parallel contextvar — everywhere that sets `ACTIVE_SCOPE` also
records `current_task()`; compare at capture. No anyio/asyncio
internals needed. The report's `!! hazard` line fires when a wedged
drain's blocker holds it via an implicit, cross-task edge — zero
false positives, because it only prints when a drain actually wedged
and was given up on.

**Rejected**: a strict sweep mode warning on *every* fork-inherited
implicit capture at construction — it fires on legitimate
enclosed-worker patterns, would need a suppression story, and the
report classification plus item 3's poisoning cover the need.

## 3. Prevention: explicit un-setting / scope-context poisoning (fxtr2)

Promoted from a doc idea to the prevention half of the design: let
disciplined regions run under a cleared/poisoned `ACTIVE_SCOPE`, so
implicit capture becomes an immediate, well-located error instead of
a latent parent edge. Candidate affordances:

- a `no_active_scope()` context manager (or sentinel value) that
  makes any implicit active-scope use underneath raise with a
  pointed message;
- a `start_task_with_scope(tg, fn, scope=...)` helper so the
  holder-task idiom (capture a controlled context, reset at task
  top — `sandbox_launcher.py`'s current hand-rolled pattern) is one
  call.

**Settled**: both affordances are wanted in principle — the helper
especially, because the scope-discipline doc can then *recommend* it,
making context hygiene an opt-in part of scope discipline rather
than a hand-rolled idiom. The rest (the "no active scope" error
story, the audit of what breaks, how far the doc pushes adoption) is
implementation detail, scoped when this graduates to its v2 plan.
Pairs with the root fix: chunk3post1 removes the known bad capture;
poisoning makes the next one loud.

**Migration obligation once this lands**: revisit
`FxtrSandboxLauncher`'s holder-task ACTIVE_SCOPE reset (currently
kept as defense-in-depth, pinned by
`tests/test_sandbox_launcher_scopes.py`). The reset *silently
rebinds* ambient to the bootstrap context — a quietly-sane default —
whereas the poisoned form makes any implicit ambient use in the
holder tasks' transitive call tree fail loudly at the offending
line, which is the stronger guarantee. Decide rebind-vs-poison for
the holder tasks then, and update the regression test to pin
whichever form wins.

## 4. Scope-discipline doc additions (v2/notes) — deferred until 1–3 land

- **Task starts fork the ambient scope context**: every
  `tg.start`/`start_soon` copies the caller's context including
  `ACTIVE_SCOPE`; a task started from a context you do not control
  (above all: a service dispatch) must not create scopes or call
  scope-consulting library code until it has reset `ACTIVE_SCOPE` to
  a value it controls — cite the helper from item 3 once it exists.
- The rule text should reference the real tools (explain-drain, the
  poisoning helper), which is why this waits.

## 5. Debugging-tips section (v2 scope notes) — deferred until 1–3 land

A short field guide in `v2/notes/scope_service_lifetimes.md` (or a
sibling note), seeded with this hunt's lessons:

- **Structure first, repro second.** For a hang involving scopes, run
  the drain report (item 1) before building behavioral
  reproductions; task/frame-level views ("everything idle, wire
  quiescent") cannot distinguish a lease cycle from designed parking.
- **Repro harnesses inherit the bug's sensitivity.** Context-
  dependent bugs demand context-controlled harnesses: every task
  start in the harness forks ambient scope state (this hunt was
  misdirected twice by exactly that — the rung-6 guest task and the
  one-sided rpc close).
- **Designed parking vs wedge**: a dispatch call scope parked in
  `__aexit__` while its caller holds returned dependent refs is the
  *designed* state; `_serve_spawn_rpc` living in drain is normal.
  Document the park-on-exit contract.
- **One-sided `open_rpc_scope` close mutually waits** with a
  still-open peer (each side holds the other's bootstrap main-service
  lease) — peers tear down concurrently.

## Sequencing

1. `chunk3post1` (the root fix) — independent, already scoped.
2. Item 1 + item 2 together (classification is report plumbing) —
   highest leverage for the whole lease-deadlock class; its v2 plan
   settles the remaining open questions above.
3. Item 3 (poisoning/helpers) — after or alongside 1, once the error
   story is designed.
4. Items 4–5 (docs) — last, so they point at real tools.
