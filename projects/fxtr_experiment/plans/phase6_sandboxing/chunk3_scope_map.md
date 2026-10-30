# Chunk 3 — scope map for the wasm e2e deadlock (investigation record)

Status: **RESOLVED.** Root cause: `FxtrSandboxLauncher`'s lazily-started
holder tasks inherited the demanding *dispatch*'s ACTIVE_SCOPE via task
context, so the long-lived sandbox-environment scope derived from an
ephemeral per-call scope, which then could never end while the process
lived — parking the dispatch's exit and everything queued behind it
(the step scope in the e2e). Fix: capture ACTIVE_SCOPE at launcher
construction and reset it at the top of each holder task
(`sandbox_launcher.py`). Verified: wasm + pyodide + container e2e, the
(d)/(e) regression pair, and the full core suite all pass. The full
hunt — including the wrong turns — is preserved below as a case study
in this lease system's failure modes; the "ROOT CAUSE" section at the
bottom is the conclusion.

## The failing flow

Test: workflow task (host-side, ephemeral binding) runs a step; the
step `ctx.connect`s a sandbox-bound key (wasm `binding_echo` guest,
one pinned dep = a host `HostPing` service behind an ephemeral
binding, config blob attached); the step calls `describe()` and
`ping_dep("pinger")` on the guest service and returns.

## Scope inventory (outer → inner, host side)

Test-owned:

- **`e2e-host`** (`host`) — test-created; holds the `HostPing` mount,
  the singleton connector for it, and the `ProbeTask` task-fn adapter
  mount. Encloses everything below in the test body's nesting.

`open_local` (bootstrap order = closing order reversed):

- **session entity scope** — `WorkflowSession.open`.
- **`wf3-runner`** (R) — isolated; `include=[entity scope,
  GLOBAL_SCOPE]`. Hosts: the user-capability mount, ephemeral
  connector refs (bridged in at bind, `independent_lease`), and —
  after the first fix attempt — dependency-spawn results.
- **AsyncExitStack inside R**: an `anyio` task group (holder tasks) +
  `launcher.aclose` pushed after it. NOTE: per
  `v2/notes/scope_service_lifetimes.md` (final paragraph), a task
  group *inside* a scope is not concurrent with that scope's drain —
  candidate restructure: tg should enclose R (see "open questions").

Launcher-owned (holder tasks on that tg):

- **env holder task** → `create_sandbox_environment` CM. Its own
  internal nesting (fxtr_sandboxing's documented lifetime tree):
  env task group (`env_tg`) → **env host scope** (active in the CM
  body; spawner mounted here) → per-spawn runner tasks on `env_tg`.
- **`wf3-sandbox:<attr>`** (H, one per launched process) — isolated;
  `include=[env host scope]`; created in a per-process holder task,
  parked on a teardown event. Holds: the transient `NixServiceDefn`
  entity; the resolved spawner proxy; the returned
  `sandbox_ref` (the `_SpawnConnectWrapperImpl` ref — the wrapper
  impl itself is mounted on the **spawn per-call dispatch scope**
  inside the env, chain-bridged into H as `spawn`'s return value).
- **RPC session for the guest** (inside `_serve_spawn_rpc`, on
  `env_tg`): `open_rpc_scope`'s documented nesting — wrapper
  lifetime/task-group → conn (`LeasedCapRPCConnection`) → rpc user
  scope (`include=[active scope at that point]`). The eryx guest runs
  on a dedicated thread of the *host* process; frames cross via JSON
  callback queues.

Executor-owned (per demand):

- **`wf3-run`** — isolated; `include=[R]`. Hosts the
  `RunCapabilityImpl` mount and the spawned task-fn ref for the run.
- **task-fn dispatch per-call scope** — the `run_task` call on the
  `TaskFnAdapter` (mounted on `e2e-host` in this test); the run
  capability proxy + all views resolve here.
- **run-capability per-call scopes** — one per engine op
  (`values_scope` in `run_step`).
- **`wf3-step`** (per step invocation, `ops_engine.run_step`) —
  isolated; `include=[run scope]`. Hosts the `StepCapabilityImpl`
  mount; the step ref is bridged in from the op's dispatch scope
  (`independent_lease`, dest-nested-in-source arrangement).
- **step-fn dispatch per-call scope** — the `proxy.run_step` call on
  the step adapter.
- **step-capability `connect` per-call scope** (`call_scope` in
  `StepCapabilityImpl.connect`) — where `BindingHost.spawn(...,
  into_scope=call_scope)` lands: the sandbox_ref bridge
  (H → call_scope, `independent_lease`), the resolved
  `SandboxConnectProto` proxy, and the returned guest service ref
  (chain-bridged in as `connect`'s return).

Guest side (inside the eryx wasm interpreter; fxtr2-portable):

- **guest host scope** — `fxtr_sandbox_rpc_adapter.main` mounts
  `_GuestMainImpl` here; peer of the host's rpc session.
- **guest per-connect dispatch scope** — one per
  `connect_with_bundle_provenance` call: args (the
  `BindingConnectRequest`: key closure, config, dep refs) are bridged
  in; the provenance c-node binds here; `_Impl(request)` (holding the
  dep ref!) is mounted here; the returned `BindingEchoProto` ref is
  chain-bridged back to the host caller.

## Explicit bridges in *our* code (everything else is framework)

1. bind: connector ref → R (`independent_lease`).
2. `ops_engine.run_step`: step ref, op dispatch scope → step scope
   (`independent_lease`).
3. `_LaunchedSandbox.connect`: sandbox_ref, H → consumer scope
   (`independent_lease`) — so the consumer can resolve the connect
   proxy and, deliberately, so the consumer's lifetime keeps the
   process alive (release-and-forget's lease).
4. (post-fix) dependency spawns: `into_scope=R` instead of the
   consumer's per-call scope.

The guest does **no explicit bridging**: it only mounts `_Impl`
holding the decoded request (whose dep `ServiceRef` arrived through
the framework's arg-bridging) — all cross-boundary lifetime machinery
is fxtr2's dependent-bridge system (`fxtr2/leases/lease_bridges.py`),
which exists precisely so a callee holding caller-side refs does not
create standing-lease cycles.

## Established facts

1. **The guest boots and works**: nix build completes, eryx runs
   (~17s CPU then idle), and — from the pre-fix task dump — the step
   *completed*: `describe`/`ping_dep` returned, the checkpoint row was
   recorded (the `run_step` body finished).
2. **Pre-fix stuck state** (asyncio task dump, all await chains):
   - `OpsEngine.run_step` suspended in the **`wf3-step` scope's
     `__aexit__`** (drain).
   - Three service-dispatch tasks suspended in their per-call scope
     CM exits (`_dispatch_body` teardown) — consistent with the
     step-fn dispatch / connect dispatch / one more, all draining.
   - Everything else idle (event loop in `select`; rpc reader idle;
     eryx thread parked in the interpreter awaiting frames; launcher
     holders parked on their teardown events — normal).
3. **The dep-anchoring fix did not resolve the hang** (same
   signature: guest boots, then idle past the fail_after window).
   The fix's reasoning assumed guest-held caller refs create standing
   leases back into the consumer scope — the dependent-bridge design
   contradicts that assumption, consistent with the fix not helping.
4. The fake-launcher suite (same funnel, same step/connect code, no
   RPC boundary) and the full 336-test core suite pass — the hang
   needs the real sandbox RPC boundary.

## Open hypotheses (to discriminate with the next dump)

- **H1 — something in the per-call scope drains waits on a guest-side
  release that never comes**: the three dispatch teardowns are the
  stuck set; what exactly does a per-call dispatch scope's drain wait
  for when its return value was chain-bridged onward and its args
  came from another scope? Needs reading
  `service_cap_common._dispatch_body` teardown + the chain-bridge
  release path, then matching against the fresh dump.
- **H2 — the guest side is wedged** (its dispatch scope never drains
  because the *guest* awaits something — e.g. a lease-release frame
  the host never sends, or vice versa): host-side dumps cannot see
  wasm-guest Python frames; would need guest-side logging or an RPC
  frame trace.
- **H3 — an ordering issue specific to `wf3-step`'s drain**: the step
  scope hosts the step capability whose `connect` per-call scope
  holds the H-lease + the guest ref; if per-call scopes of a mounted
  service drain only during the mount scope's own drain (rather than
  at call end), the step scope's drain could require exactly the
  releases H1 describes.
- (Daniel) **two nested scopes** around the connect might separate
  the lifetimes cheaply once the blocking edge is identified — keep
  as a candidate fix shape, pending the pinned-down cause.

## The connect call, hop by hop (what one `ctx.connect` builds)

`svc = await ctx.connect(key)` inside a step:

- **K_step** — the step fn's own dispatch (the engine's
  `proxy.run_step` call on the task-side step adapter).
- **K_connect** — `ctx.connect`'s dispatch on `StepCapabilityImpl`
  (mounted on the engine's `wf3-step` scope). The `key` arg crosses
  K_step → K_connect's input machinery with leases. Inside it the
  funnel (i) bridges the process's spawn ref H → K_connect
  (`independent_lease`), (ii) resolves the `SandboxConnectProto`
  proxy and calls `connect(request)` → **K_wrapper**
  (`_SpawnConnectWrapperImpl`'s dispatch), (iii) which forwards over
  the sandbox RPC to the **guest per-connect dispatch scope**
  (provenance dance, `_Impl(request)` mount).
- The returned ref is a dependent chain: guest dispatch ← (RPC) ←
  K_wrapper ← K_connect ← K_step. Unwind by design: K_step ends →
  ref released → K_connect's parked exit unparks → releases its holds
  → K_wrapper ends → guest dispatch drains. (fxtr2's `_dispatch_body`
  engineers this: call-scope exits "legitimately park while the
  caller holds returned dependent refs"; input scopes discard their
  base early so arg leases aren't hostage to the parking.)

## Elimination ladder (all in `tests/test_scope_chain_repro.py`)

Each rung runs over both raw `RPCMessage` memory streams and
JSON-lowered transports (`wrap_json_stream` — the sandbox
arrangement). Every rung **passes in ~1s**:

1. Multi-hop ref re-export over RPC (gateway hop, ± the H-bridge).
2. **Bidirectional holds** — remote-made service holds a caller-side
   dep ref while a caller child scope holds the returned ref, child
   closes mid-session (the e2e's topology; the original cycle theory
   — falsified, and the dep-anchoring "fix" was reverted).
3. The guest connect's **provenance dance** replicated verbatim
   (config walk over the dep + c-node +
   `_apply_context_marker_override`), marker-only and full-walk.
4. The **entire workflow machinery** (executor, step scope, funnel,
   safety pass) with the launched guest across a real RPC pair
   (`test_workflow_step_connect_over_rpc`).
5. Rung 4 with the **real `_GuestMainImpl` + `_SpawnConnectWrapperImpl`**
   (`test_workflow_step_connect_over_real_guest_pair`) — every line of
   host- and guest-side *software* the e2e executes, minus the wasm
   runtime itself.

Incidental discoveries (kept for the record):

- A one-sided `open_rpc_scope` close mutually waits with a still-open
  peer (each side holds the other's bootstrap main-service lease) —
  peers must tear down concurrently. The first repro harness
  accidentally built this and masked everything for a while.
- The pre-fix task dump's three "parked dispatch exits" are the
  *designed* parking, not the bug; and `_serve_spawn_rpc` living
  permanently in `__aexit__`-drain is normal (it's how the guest
  stays alive).

## Current state: cornered, not pinned

The e2e hang therefore needs something only the real runtimes add —
and BOTH wasm-family runtimes (eryx and pyodide; the fixture now
builds as both, and the e2e is parametrized over them) hang
identically, so it's a *shared* ingredient:

- the guest running the **fxtr2-portable** build (repro guest runs
  full fxtr2), and/or
- the **runtime transport pumping** (eryx callback threads /
  pyodide's deno + UDS) — though both were approximated by the
  JSON-lowered transports, which pass.

**Frame-trace finding** (temporary `FXTR_RPC_FRAME_LOG=1` tap in
`_serve_spawn_rpc`): in the hanging wasm e2e, the wire goes fully
quiescent *cleanly* — the step's calls resolve (`pong` and all),
discards and `ClosedMessage`s flow in both directions, nothing looks
outstanding — and the step scope's drain still parks. So the missing
release appears to be **host-local bookkeeping tied to the guest
connection**, not an unanswered frame.

## Experiment log (append-only; newest last)

Bottom-up rungs (in `tests/test_scope_chain_repro.py`, ~1s each,
raw + JSON transports unless noted):

- **R1–R5** (see the ladder above): all PASS.
- **R6** `test_workflow_step_connect_via_real_spawn_topology`: the
  real `_serve_spawn_rpc` + spawn dance (env tg, bridge-out-of-
  session-scope, wrapper on a real spawn dispatch scope) under the
  workflow — the workflow section PASSES through the step-scope
  teardown ("demand RETURNED"; the later env-host-close hang in the
  test is a harness artifact: in the real system the env host closes
  only alongside guest teardown).
- **Frame tap** (`FXTR_RPC_FRAME_LOG=1`, temporary, in
  `_serve_spawn_rpc` + the repro transports): the hanging e2e's wire
  goes quiescent CLEANLY (all resolves/discards/Closed flow; 118
  frames) — no unanswered frame. The passing repro's wire also ends
  quiet (73 frames; traffic legitimately differs: dep + provenance
  SCRE calls). Quiescence alone doesn't discriminate.
- **Regression range check** (Daniel's hypothesis,
  xklmstox..lxuqnlkw = the pyodide/deno 2p5 stream): the one
  RPC-relevant diff ("rpc drain fix", pkqsmkkx) is benign exception
  widening on the shutdown path — no smoking gun by inspection.
  Bisect-by-workspace deemed unnecessary after probe 1 (below)
  passed at the CURRENT rev.

Real-runtime probes (standalone scripts in the session scratchpad;
real eryx guests, warm nix cache, each does the mid-session release
twice):

- **P1** greeter, plain EntityRef config, connect in a child scope,
  close child mid-session: PASS (current rev — so no simple cleanup
  regression, and the runtime handles mid-session release).
- **P2** transporter, a **ServiceRef** as connect config (provenance
  walker invokes it in-guest; guest holds it), mid-session release:
  PASS.
- **P3** the REAL `FxtrSandboxLauncher` (holder tasks, H-bridge at
  connect) + the `binding_echo` guest + a full `BindingConnectRequest`
  (key closure + config + a live dep ref the guest calls back):
  PASS. Launcher exonerated in isolation.
- **P4** = P3 with the key persisted + served through the sqlite
  entity scope (`ensure_loadable`; the e2e's serving shape — suspected
  because the e2e's cancel log shows a live bridge lease on the
  sqlite-session token): PASS.

State after P4: every bottom-up composition passes; the e2e still
fails. Strategy flipped to **top-down bisect of the failing e2e**
(`tests/test_sandbox_e2e_bisect.py`, temporary):

- **(d)** open_local's exact bootstrap arrangement (session, runner
  scope, stack+tg+launcher inside it, BindingHost) with `host.spawn`
  driven directly — no executor/step/capability: **PASS** (twice,
  mid-session releases included).
- **(a)** the e2e's step-connect shape with a bare binding (no dep,
  no config): **HANGS** (inside the demand — same site as the e2e).
  ⇒ dep + config exonerated; the poison needs the executor/step
  *dispatch* layers driving the spawn. Combined with R5/R6 (same
  dispatch layers PASS over in-memory transports) the failing triple
  is: dispatch-scope ``into_scope`` × the real launcher × the real
  runtime.
- **(e)** (d) + the spawn performed inside a minimal service dispatch
  (``into_scope`` = the gate's per-call dispatch scope; returned ref
  re-exported to the test): **calls succeed, both child scopes close —
  the FINAL teardown hangs** (`launcher.aclose` → the process holder's
  drain → the H-leases held by the gate's dispatch call scopes, which
  never finish draining even after their callers released). This is
  the e2e's wedge relocated: in the e2e the step scope waits on its
  connect-dispatch child the same way. Pinned triple: **a per-call
  dispatch scope that received the guest ref via
  `_LaunchedSandbox.connect` never finishes draining under the real
  runtime** ((d): plain child scope → fine; R5/R6: same dispatch shape
  over in-memory transports → fine).
- **(f)** (e) minus the H-bridge: fails FAST and LOUD — the framework
  refuses the re-export (`MalformedReturn: call scope has no bridge
  for returned token …; cannot produce a chain converter`). So the
  H-bridge is what makes re-exporting the RPC-imported ref *legal*;
  the wedge is in the chain-converter/drain machinery for that
  re-export.
- **(g) — THE PIVOT**: rung 6 re-run with the guest torn down BEFORE
  the env-host close (falsifying the "one-sided-close artifact"
  explanation of its earlier hang): **still hangs, both raw and json
  transports.** The earlier artifact verdict was WRONG — rung 6 is an
  **in-memory reproduction of the wedge** (no eryx/pyodide/nix; ~60s
  per variant, arbitrarily instrumentable). The probe series (P1–P4)
  chased the runtimes because of that misattribution; their
  exonerations stand, but "needs the real runtime" is retracted.

**Revised minimal picture.** Failing fast cases: rung 6 (workflow ×
the real `_serve_spawn_rpc`+spawn topology; hangs at env-host close)
and (e) (no workflow; gate *dispatch* consumes the connect; hangs at
`launcher.aclose`/holder drain). Passing neighbors: rung 5 (identical
software, but the wrapper mounted on a plain holder scope and the rpc
session opened in a neutral task context) and (d)/P3 (plain child
scopes consume the connect). Unresolved tension to bisect: P3 (real
spawn topology + eryx, plain-scope consumer) passes its full teardown
while (e) (same but dispatch-scope consumer) hangs — so both the
spawn-side topology AND the dispatch-scope consumer seem necessary
together. Prime structural suspect: in the real spawner, the guest
RPC session task is started from *inside* `spawn`'s per-call dispatch
context (`env_tg.start(runner)` inherits it), so the long-lived rpc
session scope derives from an ephemeral per-call scope; combined with
a dispatch-scope consumer whose return chain-converter routes through
those tokens, some link never releases.

Rung-6-vs-passing bisect (each ~1s, raw+json):

- **(i)** serve task started from a neutral context (not spawn's
  dispatch): still hangs — context inheritance exonerated.
- **(ii)** wrapper mounted on env-host instead of the call scope:
  inconclusive (tripped an unrelated `not found in any parent scope`
  arena error in the rearranged bridge; parked for now).
- **R7** rung 5 + a parked dispatch hop in the ref's *chain*
  (getter returns a pre-existing ref): PASSES.
- **R8** R7 with the wrapper *mounted on* the parked dispatch scope
  (spawn's mount shape): PASSES.
- **R9** R8 with the rpc session in `_serve_spawn_rpc`'s DRAINING
  state (CM parked in `__aexit__`, guest alive via bridges): PASSES.
- **R10** next: R9 with the parked dispatch's mount scope moved to a
  separate env-host-like scope (consumers reach it via includes) —
  the last structural delta to hanging rung 6.

- **R9** = R8 + the draining-session state: PASSES. **R10** = R9 +
  the parked dispatch homed on a separate env-host scope: PASSES.
- **Latency injection** (10ms per frame on the in-memory transports,
  R9): PASSES — the lost-wakeup-race theory (at that latency) fails.
- **RUNG-6 RETRACTION**: rung 6's hang was ANOTHER artifact — its
  guest task was started from *inside* the spawn dispatch, so the
  whole in-memory guest contextually descended from the parked call
  scope (an edge no real system has). With the guest started from a
  neutral context, rung 6 PASSES. Consequence: **every in-memory
  replication passes**; the genuine hangs (the e2e, bisect (a),
  bisect (e)) all involve a real eryx guest. "In-memory case in hand"
  is retracted.
- **(e) instrumented under eryx** (task dump at the hang): the two
  gate `get()` dispatches + one more dispatch sit parked at
  `service_cap_common.py:1416` (the call-scope CM exit that waits for
  the call token to drain) **after both consumer child scopes closed
  and released** — their user methods completed minutes earlier; some
  lease on their call tokens never ends. `launcher.aclose` →
  `_launch_holder`'s H drain → those H-leases queue behind them. The
  wire (from the earlier frame trace) is quiescent and complete, so
  the outstanding lease is host-local — but only materializes with a
  real guest on the far side.

## The minimal pair (current best handle)

`tests/test_sandbox_e2e_bisect.py`, both real-eryx, ~40s each, warm:

- `test_bindinghost_spawn_direct_no_executor` (**d**): consumer = a
  plain child scope → everything drains, full teardown PASSES.
- `test_spawn_inside_service_dispatch` (**e**): consumer = a service
  dispatch's per-call scope (the ref re-exported as its return) →
  the dispatch exits never finish draining; teardown hangs.

One flipped ingredient: whether the guest-connect result is consumed
(and re-exported) by a per-call dispatch scope. In-memory equivalents
of BOTH pass, so the eryx guest contributes something further that
pure host-side reasoning hasn't identified — likely visible only via
lease/token introspection on the parked call tokens at hang.

## ROOT CAUSE (found via lease introspection)

Instrumentation: fxtr2's existing per-lease allocation-traceback
capture (`LEASE_LEAK_TRACK_ALLOCATIONS`) + a scope registry
(`tests/_debug_taskdump.py::install_scope_lease_tracking` /
`dump_scope_leases`) dumping each live scope's outstanding output
leases at the (e) hang.

The dump showed the launcher's **environment scope** (`sandbox_env`,
created by `create_sandbox_environment`) carrying the gate's
**per-call dispatch scope** (`…/call/get/derived`) among its derived
lifetime's *input grants* — i.e. the long-lived env scope (and
transitively the per-process holder H and the spawn scopes) **derives
from the ephemeral dispatch scope**.

Mechanism: `FxtrSandboxLauncher` starts its env / per-process holder
tasks lazily via `tg.start(...)` from inside whatever *dispatch* first
demands the binding; anyio task-context inheritance carries that
dispatch's ACTIVE_SCOPE into the holder task, where the environment's
(non-isolated) scopes pick it up as a derived parent. The per-call
scope then cannot end while the process lives → its exit parks forever
→ the step scope (e2e) / teardown ((e)) queue behind it.

Why every earlier probe missed it: (d)/P1–P4 launch from the *test
task* (harmless ambient scope); all in-memory rungs used stand-in
launchers with no lazy scope creation inside dispatches; rung 6's
"(i) neutral serve-task" variant neutralized the wrong task (the rpc
serve task, not the env/launch holder tasks). The runtimes were never
the variable — the e2e/(a)/(e) always launched from inside a dispatch.

**Fix** (in `sandbox_launcher.py`): capture `ACTIVE_SCOPE` at launcher
construction (the bootstrap's context) and reset it at the top of
`_env_holder` and `_launch_holder`, so holder-task scopes derive from
the bootstrap context, never the demanding dispatch. Verified: (e) and
(d) pass (~38s, real eryx, mid-session releases + full teardown).

## Verification and disposition

- Fixed and verified: wasm e2e (deps + config + in-flight-survives-
  shutdown), pyodide variant, container task-fn e2e (one fixture fix:
  the missing `fxtr-service-runner = ["hatchling"]` build-dep hint),
  the bare-binding bisect, the (d)/(e) minimal pair, the full core
  suite (370) + pyright.
- The temporary frame tap in fxtr_sandboxing's `_serve_spawn_rpc` is
  reverted; the dead-end bisect arms are pruned.
- **Post-review disposition.** The regression is pinned at core-suite
  cost by `tests/test_sandbox_launcher_scopes.py`: the real launcher
  over a fake `create_sandbox_environment` that preserves the hazard
  ingredient (a non-isolated host scope), driven from inside a real
  dispatch — verified to deadlock with the launcher's ACTIVE_SCOPE
  reset reverted, and the `fail_after`-guarded e2e tests back it up
  with the real runtimes. Everything else from the hunt — the repro
  ladder (`test_scope_chain_repro.py`), the e2e bisect variants incl.
  the (d)/(e) real-eryx pair (`test_sandbox_e2e_bisect.py`), the
  debug helpers (`tests/_debug_taskdump.py` — task dump + scope-lease
  dump), their `fxtr-sandbox-rpc-adapter` dev dep, and a minimal
  in-memory two-test example of the failure class
  (`tests/test_dispatch_scope_inheritance.py`) — lives in a
  branching, deliberately-abandoned `debug residue` commit on top of
  the chunk-3 commit: reference material, never built upon. (The
  probe scripts lived in the debugging session's scratchpad and are
  not preserved.) The scope-lease-dump helpers are the promotion
  candidate for real fxtr2 tooling — see the safeguards plan below.

## Follow-ups (planned as chunk3post files)

- **The root enabler is a scope-discipline violation in
  `fxtr_sandboxing`**: `create_sandbox_environment`'s host scope
  (`FxtrScope(name="sandbox_env")`, `spawner.py`) is non-isolated in
  library code, so it silently adopts whatever scope is ambient as a
  structural parent — the launcher-side reset treats the symptom.
  Fixing it (isolated + explicit includes, keeping the documented
  active-in-body contract explicit) and auditing the package's other
  non-isolated sites is `chunk3post1_sandboxing_scope_fix.md`.
- The safeguard candidates raised by this hunt — explain-drain /
  drain-watchdog tooling in fxtr2 (the lease dump that found the
  bug, as a first-class affordance), debug-mode detection of
  long-lived scopes deriving from per-call dispatch scopes, the
  scope-discipline doc additions (task starts fork the ambient scope
  context) and a debugging-tips section, incl. the two incidental
  discoveries (one-sided rpc-close mutual wait; the dispatch
  park-on-exit contract) as `v2/notes/scope_service_lifetimes.md`
  doc lines — are collected for discussion in
  `chunk3post2_scope_safeguards.md`.
