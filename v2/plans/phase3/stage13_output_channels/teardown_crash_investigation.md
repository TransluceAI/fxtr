# Investigation: demo-server crash on session close (wasm + channels)

Status: **fixed** (2026-06-12). All three issues traced (three
fork-agent passes over the logs + code), red tests written (xfail
strict), then fixed — two commits per issue (fix + test tightening):

1. **Issue 3** (mangled diagnostic): `_phase1_resolve_our_export`'s
   violation messages now name the referencing expression and the
   expected entry kind verbatim; test pins the exact wording.
2. **Issue 1** (break/push race): `on_break` records a tombstone
   `(grant, reason)` keyed by export id before teardown
   (`_broken_grant_export_tombstones`; ids are never reused, so
   tombstone-absence still means "never had" → abort). Phase 1
   consults the tombstones on a missing grant export and substitutes
   a fresh **already-broken lease on the original grant**
   (`make_broken_lease`, new in `leases/local_lease.py`) — the
   requester gets the request-scoped `LeaseBrokenError` it already
   handles, via dispatch `take()` → the per-push rejection path.
   Tombstones drop on the peer's `DiscardMessage` (its break
   acknowledgment); otherwise retained for the connection's lifetime.
   See the BreakMessage section of `v2/fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`.
3. **Issue 2** (blast radius): every `_spawn_runner_*` body is wrapped
   in `_contain_failures_after_started` (`fxtr_sandboxing/spawner.py`)
   — any `Exception` after `task_status.started()` is logged and
   contained to that spawn (catch-all by design: no guest misbehavior
   should crash the env); pre-`started` failures still ride back as
   the spawn invoke's error; cancellation always propagates. The
   shared open-rpc/park tail is factored into `_serve_spawn_rpc`.

With fixes 1+2 the observed scenario closes cleanly (issue 1 makes
the colliding unsubscribe degrade request-scoped; issue 2 contains
any future guest-fatal path). The "Open questions" below remain open
— they affect framing/urgency, not the fix.

Original investigation follows.

## Symptom

Reproduced twice, deterministically, during the stage-13 headless e2e
runs: calling `close_session` on a workstation session holding
spawned wasm schedulers + channel-storage refs + callback-inbox
subscriptions crashes the *entire demo server* (exit 1) instead of
riding out the teardown.

- Part-3 log (simple_scheduler): `/tmp/fxtr-stage13-demo-teardown-crash.log`
- Part-4 log (streaming_scheduler): `/tmp/fxtr-stage13-part4-teardown-crash.log`

Each log contains exactly **one** fatal export id (`-69` in part 3,
`-66` in part 4); the repeated "no such export" strings are the same
abort re-printed through nested exception-group reprs.

## Which lease breaks (the direct answer)

**The guest-side backing lease of the grant export that keeps one
subscription's `UnsubscribeHandle` alive for the host.** Chain of
custody:

- When an inbox parker subscribes (`fxtr_workstation/callback_impl.py:329`),
  the guest returns the `UnsubscribeHandle` ref; the return path
  exports a grant on the handle's subject token — guest table id
  `-66` / `-69` (negative ids are allocated by the exporting side,
  `lowering_devaluate.py:492`). The guest holds the grant's
  **backing lease** (`GrantExportEntry.backing_lease`, taken at
  `lowering_devaluate.py:499-506`); the host holds the matching
  import (`RemoteGrant`, minted at `lowering_evaluate.py:190`).
- The fatal message is structurally pinned to
  `handle_proxy.unsubscribe()`: its lease arg devaluates via the
  **bounce-back branch** (`lowering_devaluate.py:467-469` — lease's
  grant is a `RemoteGrant` on this connection → emit
  `ImportedLeaseExpr(grant_import_id=-66)`). The only lease the host
  bounces back at that point is the one on the request's target —
  the unsubscribe handle. (Near-certain by structure; not directly
  logged.)
- **What severs it** (round-3 refinement): NOT the polite teardown —
  the hub's **crash backstop**. An in-flight event delivery's
  host-side result died during the close; the subscription worker hit
  the delivery-failure `except` (`subscription_util.py:662-677`) and
  called `entry.cancel_holder()`, force-closing `hold_scope` → the
  handle-grant's backing lease breaks → `on_break`
  (`lowering.py:2033-2080`) sends `BreakMessage` and removes the
  export entry immediately (deliberately — "if we kept the export
  entry around waiting…drain could block indefinitely"). The host's
  polite `unsubscribe()` for that same subscription crossed the
  `BreakMessage` on the wire → the guest's
  `_phase1_resolve_our_export` finds nothing → connection abort.

Telling detail: the bounce-back path already defends against the
*discard* variant of this race (`lowering_devaluate.py:457-466`
takes the lease "so the peer [can't] tear down the export the
`ImportedLeaseExpr` references before phase-1 can resolve it") — it
just has no defense against the **break** variant, where the *owner*
tears the export down for its own reasons. Supporting evidence that
issue 1 is an overlooked case, not a deliberate choice.

## Is breaking expected here? Does the politeness window race?

**The politeness window is sound and was upheld** (round-3 finding —
this refines the earlier "break is just the design" framing):

- Per-parker, `unsubscribe()` is awaited (bounded by
  `UNSUBSCRIBE_TIMEOUT_SECONDS`, shielded) *before* that parker's
  `force_close` (`callback_impl.py:344-368`) — no intra-parker race.
- Hub-side, the polite path provably leaves the holder alive: the
  worker's unsubscribe exit (`subscription_util.py:650-652`) returns
  without touching the holder ("holder is left to its natural
  drain"), and the reply-safety docstring
  (`subscription_util.py:344-346`) is honored on that path.
- What defeated it is the **crash backstop** — on a failed delivery
  the worker deliberately cancels the holder
  (`subscription_util.py:670-677`), breaking the peer's handle
  grant; that's its documented purpose (the durable teardown
  signal). The backstop fired for one subscription whose in-flight
  delivery died during the close, and its break collided cross-wire
  with that subscription's in-flight polite unsubscribe.
- Breaks are therefore expected **only on the backstop/force
  paths**; an owner can't cleanly release a lease the peer holds
  (release is holder-initiated), so break is the correct mechanism
  there — and the push path must tolerate it.

**Single trigger explains all five failures.** The four
`LeaseBrokenError` unsubscribe failures are **collateral of the one
abort**, not independent races: `_abort` (`lowering.py:918-995`)
marks every imported token ended; on the host, `_on_abort` runs
`conn._abort(..., notify_peer=False)` before re-raising
(`lowering_message_handlers.py:950-952`), so every pending
push-result token import flips its `await_resolution` to
"discarded" → the exact "result lifetime ended" message, four times
(slots 10/13/16/18); the fifth manifests as the abort itself.
4 + 1 = 5 subscriptions. (The log prints the four warnings before
the abort traceback only because the traceback surfaces when the
task groups unwind.)

What the reply rides, for reference: the requester's `dispatch()`
allocates result token/future/grant imports
(`lowering_remote_proxies.py:462-530`); the responder's reply
lifetime is the push-result token (`_on_push`,
`lowering_message_handlers.py:154-230`) — independent of
`hold_scope`, so the reply path itself needed no fix.

**Consequence**: with fix option 2 (push-path tolerance) alone, this
exact scenario becomes a fully clean close — the colliding
unsubscribe gets a request-scoped `LeaseBrokenError` (which the
parker already logs and survives), and the other four succeed.
Remaining unknown (one hop): what killed the host side of that first
in-flight delivery during the sync close pass (inbox `shutdown_event`
→ `impl_scope` teardown cancelling the dispatch vs. session
event-delivery internals) — it decides whether close *always* trips
the backstop (observed 2/2) or only when a delivery is in flight;
mechanically nothing downstream changes.

## Reconstructed timeline (refined)

1. During the runs (not the teardown): 19–23×
   `_encode_fxtr_id ... provenance parent` warnings (see "Secondary
   notes").
2. `close_session` → `_close_session_drain_trackers`
   (`fxtr_workstation/state.py:2726`, **synchronous**, inside
   `apply_mutation`): (a) `mark_closed()` every inbox — wakes all
   parkers; (b) in the same sync pass, drains the session's
   entity/service refcounts (clean discards of the scheduler +
   channel-storage refs).
3. The parkers (5 in part 4) then race each other, each doing
   best-effort `unsubscribe()` then `sub_scope.force_close()`
   (`callback_impl.py:344-368`). Each `force_close` kills that
   parker's subscriber mount + source bridge. Guest-side, in-flight
   hub deliveries to dead subscribers break → the hub drops those
   subscriptions → per-subscription scopes die (and after the last
   parker bridge drops, the mount drains and `hub.aclose()` reaps
   the rest) → the *other* still-host-held unsubscribe-handle grants
   break → `BreakMessage`s.
4. One subscription's in-flight delivery dies host-side → the guest
   worker's crash backstop cancels that subscription's holder
   (`subscription_util.py:670-677`) → handle-grant break +
   `BreakMessage`.
5. That subscription's polite `unsubscribe()` crosses the
   `BreakMessage` on the wire: its `HandleRequestExpr` arrives
   referencing the break-torn-down grant → guest
   `phase1_protocol_violation` → AbortMessage + guest
   `RPCConnectionAborted`. The other four unsubscribes' replies then
   break as **collateral of the abort** (host-side `_abort` ends
   every imported result token → four `LeaseBrokenError: result
   lifetime ended` warnings, handled "tearing down anyway").
6. Host reader loop receives the abort → `_on_abort` re-raises
   (`lowering_message_handlers.py:952`) → propagates out of
   `_spawn_runner_nix_wasm` (`fxtr_sandboxing/spawner.py:254`)
   through `run_in_factory` (`runner.py:555/559`) into
   `create_sandbox_environment`'s task group (`spawner.py:554`) →
   **unhandled** → hypercorn lifespan failure → server exit 1.

## The three issues

### Issue 1 — RPC push path lacks the teardown-race tolerance the pull/discard paths have

The handlers module's doctrine (`lowering_message_handlers.py:15-37`)
distinguishes state we've **never had** (→ abort, peer bug) from
state we **no longer have** (→ teardown race, tolerate). `_on_pull`
(~line 273) and `_on_discard` implement that tolerance for exactly
the grant-break case; `_phase1_resolve_our_export`
(`lowering_evaluate.py:316-321`) does not — any missing export on
the push path is a connection-fatal protocol violation. Given breaks
are the designed owner-side drain semantic (above), the push path is
not upholding the layer's own invariant doctrine.

This is an fxtr2 `rpc/lowering` asymmetry, not a stage-13 channels
bug per se. Pre-existing vs stage-13 (medium confidence
pre-existing): every ingredient exists for the pre-channel
simple_scheduler (one inbox subscription + close_session); stage 13
multiplied subscriptions (5 vs 1) and guest mounts, plausibly turning
a rare race deterministic (2/2). Confirm via a pre-3.13.1 scaffold
A/B (subscribe → close_session).

### Issue 2 — blast radius: one guest abort kills the whole server

Independently of issue 1: a single guest connection dying during
drain should terminate *that spawn*, not collapse the shared sandbox
environment and the server lifespan. `RPCConnectionAborted` escapes
`_spawn_runner_nix_wasm`'s task group uncontained — today any guest
protocol hiccup is server-fatal.

### Issue 3 — the mangled diagnostic (reporting-path bug)

`ImportedrantExportEntryExpr` is confirmed mangled:
`lowering_evaluate.py:318-320` builds the message as
`f"Imported{kind.__name__[1:]}Expr"` — the `[1:]` expects a
leading-underscore class name, but `kind` is `GrantExportEntry`, so
the `G` gets chopped. Small fix; also a smell that this
violation-reporting path wasn't exercised — consistent with issue 1
(the path conflates "never had" with "no longer have").

## Fix options (ranked)

1. **Containment in `fxtr_sandboxing/spawner.py`** (cheap; do
   regardless): catch `RPCConnectionAborted` (arguably any per-spawn
   exception) around the `open_rpc_scope` body in
   `_spawn_runner_nix_wasm`, marking that spawn dead instead of
   killing the environment task group.
2. **Root-cause tolerance in phase 1**: in
   `_phase1_resolve_our_export`, for `kind=GrantExportEntry` treat a
   missing entry as a *request-scoped* failure — e.g. substitute an
   already-broken lease (the sender then gets the `LeaseBrokenError`
   it already handles), or keep short-lived tombstones of
   break-torn-down grant ids so "no longer have" (tolerate) stays
   distinguishable from "never had" (abort). Mirrors the
   `_on_pull`/`_on_discard` precedent. Also covers the same race on
   healthy paths (a source revoking a grant while a caller's request
   is in flight).
3. **Workstation close-ordering** (mitigation only, and now known to
   be insufficient): the trigger was an *already in-flight* delivery
   dying — reordering unsubscribe-before-release would not have
   prevented the backstop firing. Also conflicts with the design
   (`close_session` teardown runs in the synchronous commit phase,
   `state.py:2726`, and must not block on N guest round-trips).
   Keep ranked below 1+2; option 2 alone makes this scenario fully
   clean.
4. **Fix the diagnostic** (issue 3): render the entry kind correctly
   in `phase1_protocol_violation`'s message.

## Red tests (written 2026-06-12 as `xfail(strict=True)`; un-xfailed + tightened with the fixes — now the regression tests)

- Issue 1 (narrow):
  `v2/tests/test_rpc/test_devaluate_phase1.py::TestPhase1BreakRaceTolerance`
  — freezes the cross-wire race deterministically (no readers run in
  that harness, so the `BreakMessage` sits unprocessed): A exports a
  grant on a revocable lifetime, B imports, A's lifetime revokes
  (real `on_break` teardown), B bounces its still-live-from-its-view
  lease back, `a._phase1_substitute` → today aborts; asserts the
  desired request-scoped degradation (an already-broken lease).
  A wire-level variant (hold back the `BreakMessage` in a
  `test_lowering.py`-style duplex harness; assert the connection
  survives) is a worthwhile follow-on once the fix lands.
- Issue 2:
  `projects/fxtr_sandboxing/fxtr_sandboxing/tests/test_spawner_blast_radius.py`
  — **mocked guest, real everything else**, deliberately independent
  of issue 1. Only `_run_wasm` (the spawner's single contact with
  eryx) is replaced: an in-memory pipe whose far end runs the
  adapter's own `_GuestMainImpl` over real `JSONObjectStream`
  framing and a real `open_rpc_scope` (mirroring
  `fxtr_sandbox_rpc_adapter._main.main`), so `spawn()`, the runner,
  the wire frames, the handshake/drain, `connect` with provenance,
  and the environment task group are all unmodified production code.
  Injection site: the doomed guest connection's own
  `_abort(reason)` — the primitive every guest-side fatal path
  funnels through — which emits a genuine `AbortMessage` through the
  guest's writer; the host reader's `_on_abort` raise and the
  propagation after it are the real failure path. Two mock spawns
  (healthy + doomed); asserts the healthy sibling + environment
  survive. Today the env task group collapses; the test xfails in
  ~0.3 s. (Two earlier drafts: a poison-push variant that conflated
  issue 1 — fixing either issue would have flipped it — and a
  host-side receive interceptor that skipped the wire and the guest;
  both replaced by this one. Earlier apparent "hangs" were test
  bugs — an unheld spawn ref letting the doomed spawn tear itself
  down cleanly before injection — not a product wedge.)

  Useful lifecycle fact surfaced while building it: spawn runners do
  NOT park for the sandbox lifetime — `spawn()` itself sets
  `teardown_event` right after bridging the guest ref out
  (`spawner.py:309-...`), so the runner body exits at spawn time and
  the connection's lifetime is governed by `open_rpc_scope`'s **exit
  drain**, pinned by the foreground bridge. The demo's aborts
  therefore hit connections sitting in that drain.
- Issue 3:
  `v2/tests/test_rpc/test_devaluate_phase1.py::TestPhase1ViolationDiagnostics`
  — never-had id (stays connection-fatal after the issue-1 fix);
  asserts the message names `GrantExportEntry` (pre-fix:
  `ImportedrantExportEntryExpr…`). Tightened to the exact implemented
  wording with the fix.

## Open questions

- The pre-stage-13 A/B (affects urgency framing only; mechanism is
  established).
- What killed the host side of the first in-flight delivery during
  the sync close pass (the backstop's trigger; decides whether close
  always trips this or only with a delivery in flight).
- Whether the same break/push race is practically reachable on
  healthy paths (source-initiated revocation mid-request) — fix
  option 2 covers it either way.

## Secondary notes

- The 19–23× `_encode_fxtr_id received an ID with a provenance
  parent` warnings come from
  `v2/fxtr2/services/cap_ir_arena.py:136-144` — a deliberately
  downgraded assert with an existing
  `TODO(daniel): Debug why this still triggers under new provenance
  flow.` Pre-existing; the channel retention copies (task outputs
  carry provenance parents) likely just amplify the count.
- The single `Lease leaked without discard`
  (`rpc-conn/<uuid>/derived`) at close time looks like an independent
  host-side leak on the connection-teardown path — possibly worth its
  own small look; not chased here.
