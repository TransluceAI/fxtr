# Extra 6: Devaluator-path resolve-obligation bug

## Problem

`LeasedCapRPCConnection` mints one resolver task per future export that
ever owes a `ResolveMessage`. That resolver task is the only code path
that calls `_enqueue_resolve_for` / `_enqueue_forwarding_resolve_for` /
`_enqueue_reject_for`. Today the resolver is only spawned from
`_on_pull` on the 0→1 transition of
`FutureExportEntry.resolve_obligation_count`. That leaves a gap: when
the obligation count transitions 0→1 via the *devaluator* path
(`lowering_devaluate._bump_await_future_obligation`), no resolver is
spawned, and depending on what the peer does next the obligation may
never be discharged.

### The intended invariant

The lowering layer's obligation accounting is symmetric across the two
sides of the wire. From `FutureExportEntry.resolve_obligation_count`'s
docstring:

> Running tally of "please resolve" obligations we owe to the remote …
> **+1 per incoming `PullMessage`, +1 per outbound `AwaitFutureExpr`
> the devaluator emits referencing this export** (each such expression
> implicitly pulls on the exporter). Decremented by the refcount in
> incoming `UnpullMessage`s; zeroed when we emit our `ResolveMessage`
> / `RejectMessage`.

From `UnpullMessage`'s docstring (the symmetric importer-side rule):

> The sender has *received* an `AwaitFutureExpr` from the exporter
> referencing this future — the only way for the sender to evaluate
> such an expression is to pull the future, so each received
> occurrence obligates the exporter to deliver a resolution.

Composed, the layer's contract on the exporter side reads:

**Whenever `resolve_obligation_count > 0` and `not resolved_sent`, the
exporter owes a `ResolveMessage` for that future export.** It must be
delivering one — i.e., a resolver task must be alive — until the
obligation count drops back to zero (via `UnpullMessage`) or a Resolve
/ Reject is emitted.

That invariant is the load-bearing one for the part of the protocol
that lets the importer suppress redundant `PullMessage`s.
`RemoteIRFuture.wait` skips emitting a `PullMessage` when
`resolve_obligation_refcount > 0` (because phase-1 substitute already
bumped it on receipt of an inbound `AwaitFutureExpr`). The importer is
correct to skip — the protocol says the exporter is already obligated
because of the `AwaitFutureExpr`. But the exporter only honors that
obligation if a resolver actually runs.

### How the current design fails the invariant

There are two call sites for `_bump_resolve_obligation_count`:

1. `_on_pull` (lowering_message_handlers.py): bumps on every incoming
   `PullMessage`, and spawns a resolver on the 0→1 transition.
2. `_bump_await_future_obligation` (lowering_devaluate.py): bumps on
   every outbound `AwaitFutureExpr` whose `future` slot is one of our
   exports, and **does not spawn a resolver**.

The `_on_pull` gate that decides whether to spawn is

```python
conn._bump_resolve_obligation_count(future_entry)
if future_entry.resolve_obligation_count > 1:
    # A resolver is already running; additional pulls are accumulated
    return
# ...spawn resolver...
```

The comment encodes the intent — "a resolver is already running, so
just accumulate" — but the *check* (`> 1` after the bump) is a proxy
for that, valid only if every bump that increases the count from 0
also spawns a resolver. The devaluator path violates that proxy.

Consequence: any time the devaluator bumps a future export's
obligation count and **no incoming `PullMessage` ever lands on a 0→1
transition**, the obligation is undischargeable. Two concrete shapes
that hit this:

- **The peer suppresses its Pull.** Phase-1 substitute on the peer
  bumps `resolve_obligation_refcount` for the inbound
  `AwaitFutureExpr`. When peer-side code subsequently calls
  `RemoteIRFuture.wait`, it sees `resolve_obligation_refcount > 0` and
  skips its `PullMessage` (correctly, per protocol — the obligation
  was pre-paid). No Pull ever arrives on our side; no resolver spawns;
  the peer's wait blocks forever.
- **A second devaluator bump races in between Unpull and Pull.** If
  the peer's phase-1 obligation is released via `UnpullMessage` before
  the peer's wait fires its Pull (the typical single-push case), and
  the exporter devalues a second `AwaitFutureExpr` referencing the
  same export in the gap between those two messages, the Pull arrives
  while the count is already 1 from the second devaluator bump. The
  `_on_pull` gate sees `> 1` and skips. No resolver runs.

### Red test

`tests/test_rpc/test_lowering.py::TestOutboundAwaitFutureExprOnLocalFutureExport::test_devaluator_bump_alone_must_produce_resolve`
is the minimal direct statement of the invariant. The body:

1. Brings up `LeasedCapRPCConnection.as_client(a_transport)` and feeds
   a `HelloMessage` back so A's task group is fully alive.
2. Spawns a local `IRFutureLocalImpl` `F` via
   `spawn_ir_future_in_task_group`.
3. Calls `their_main_interface.dispatch(UnwrapForPipelining(F,
   F_lease), Call("echo", "hi"))`. The devaluator emits
   `HandleRequestExpr` whose `.lease` is `AwaitFutureExpr(future =
   ExportedIRFutureExpr(F_export_id), …)` and bumps
   `resolve_obligation_count` 0→1 on F's fresh export entry.
4. Reads the outbound `PushMessage` off A's transport and extracts
   `F_export_id`.
5. Asserts that a `ResolveMessage(future_export_id=F_export_id)` shows
   up on A's outbound within a short timeout.

No `PullMessage` is injected at any point. Per the invariant, A is
already obligated to resolve from step 3 onward. The test is currently
marked `@pytest.mark.xfail(strict=True, ...)` and times out at the
inner `anyio.fail_after(2.0)`. Tracing with `FXTR_REPRO_TRACE=1` shows
no `_on_pull` events at all — the devaluator bump never gets paired
with a spawn.

When the fix lands, xfail-strict will surface the unexpected pass and
the marker should be removed.

### Downstream surface — the workstation hang

This was first observed as an "uppercase invoke hangs" report in the
`fxtr_workstation_demo`: an agent in one session asks a spawned
sandbox service to do a trivial call while a second observer (a
frontend panel) is open against the same workstation. The first
session's invoke hangs indefinitely.

The hang was bisected through repros that progressively stripped away
the sandbox, the MCP plugin, persistence, and the chain-bridge
machinery. What's left is exactly this RPC-layer bug, in the topology
where a cap-imported `ServiceRef` is invoked by two sessions
concurrently and the workstation's
`_deliver_result_as_event` 3-phase pipeline emits multiple back-to-
back outbound `AwaitFutureExpr`s referencing the same future export
on the host side of the chain bridge's mock-RPC connection.

To reproduce at the workstation level, drive two `/client` sessions
against a workstation hosting a sandboxed service whose ref is
cap-imported (e.g. via `SandboxSpawnerImpl`-style chain bridge), have
both sessions invoke methods on that ref concurrently, with at least
one `~5ms+` delay between the second session's `TrackServiceOp` and
its `InvokeOp`. The bug fires when the host's second outbound
`AwaitFutureExpr` for the chain bridge's pipelined future lands in
the SKIP-SPAWN window of the first observer's already-in-flight
`_on_pull`.

The red test above pins the same gap at the bare RPC layer — no
workstation, no chain bridge, no MCP — and demonstrates it directly
from the invariant.

## Fix shape

The root cause is that the spawn/cancel of the resolver is not
mechanically coupled to the count bump/decrement that ought to imply
it. We fix this by making the count mutators on
`LeasedCapRPCConnection` the *sole* mutators of
`resolve_obligation_count`, and folding the spawn (on 0→1) and the
cancel (on N→0) into them. After the fix:

- The invariant "ROC > 0 ∧ ¬resolved_sent ⟹ a resolver task is alive,
  with `resolve_task_cancel_scope is not None`" is mechanically
  enforced by the count mutators themselves.
- Every caller that nudges the count goes through one of two
  primitives; none of them needs to know about resolver lifecycle.

### The two primitives

Rename and generalize the existing helpers on
`LeasedCapRPCConnection`:

```python
def _increment_resolve_obligation_count(
    self, future_entry: FutureExportEntry, *, count: int = 1
) -> None:
    """Add ``count`` to the obligation count; on a 0→positive crossing
    (and ``not resolved_sent``), spawn a resolver task.
    """

def _decrement_resolve_obligation_count(
    self, future_entry: FutureExportEntry, *, count: int = 1
) -> None:
    """Subtract ``count`` from the obligation count; on a positive→0
    crossing (and ``not resolved_sent``), cancel the resolver task.
    Clamps overshoot via the existing protocol-violation path.
    """
```

These replace `_bump_resolve_obligation_count` and
`_undo_bump_resolve_obligation_count`. The connection holds the
"ROC>0 ⇔ resolver alive" invariant. Every call site degrades to a
straight count nudge.

Call-site changes:

- `_on_pull` (lowering_message_handlers.py): drop the explicit spawn,
  drop the `> 1` accumulation gate, drop the spawn body. Just look up
  the entry, short-circuit on `resolved_sent`, call
  `_increment_resolve_obligation_count(entry)`. Done.
- `_bump_await_future_obligation` (lowering_devaluate.py): replace
  `conn._bump_resolve_obligation_count(entry)` with
  `conn._increment_resolve_obligation_count(entry)`. The `ctx.undo_log`
  entry stays a single lambda that calls
  `_decrement_resolve_obligation_count(entry)`; cancel-on-undo is
  handled inside `_decrement_…` automatically when the rollback
  pushes the count back to 0.
- `_on_unpull` (lowering_message_handlers.py): replace the inline
  decrement-and-cancel body with
  `_decrement_resolve_obligation_count(entry, count=msg.refcount)`.
  The over-decrement / `phase1_protocol_violation` clamp lives inside
  the primitive now.

Note: `_clear_resolve_obligation_on_send` (the connection method
called from `_enqueue_resolve_for` / `_enqueue_reject_for` /
`_enqueue_forwarding_resolve_for` when we *are* emitting a
Resolve/Reject) deliberately bypasses the cancel path — the caller is
the resolver itself, mid-emission. That site keeps zeroing the count
and dropping from the active-export set directly; it does *not* route
through `_decrement_resolve_obligation_count`. Document this
asymmetry on the helper.

### The spawn helper

Factor the existing `_on_pull` spawn body out as
`_ensure_resolver_running_for(future_entry: FutureExportEntry)`, called
from `_increment_resolve_obligation_count` on the 0→positive crossing
when `not resolved_sent`. The helper:

- Asserts `future_entry.resolve_task_cancel_scope is None`.
- Looks up the grant export entry needed for `backing_lease` (see
  next subsection).
- Clones `backing_lease` twice (`wait_lease` + `survival_lease`),
  identical to today's `_on_pull` body.
- Builds the `resolver` / `resolver_with_scope` coroutines verbatim.
- Stores the cancel scope on the entry.
- `tg.start_soon(resolver_with_scope, name=...)`.

The cancel-side helper (called from
`_decrement_resolve_obligation_count` on the positive→0 crossing) is
trivial enough to inline: clear `resolve_task_cancel_scope`,
`scope.cancel()`. The drop from `_active_resolve_obligation_exports`
happens in the primitive itself, separately from the cancel.

### Looking up the grant entry from the export

`_on_pull` today reads `msg.grant_import_id` to find the grant entry
that owns `backing_lease`. The devaluator path doesn't have a message
to read it from. Options:

  (a) Add `grant_export_id: int` to `FutureExportEntry`, populated at
      every export-registration site
      (`_register_future_export` / `_devaluate_future` / `_on_push`).
      Cleanest; semantics are obvious; paid once.
  (b) Reverse-lookup via `token_export_id`. Hacky.
  (c) Stash the `backing_lease` source on the future entry. Redundant
      with the grant entry but avoids the lookup.

**Recommend (a)**, same as the previous draft of this plan. Add it as
a first step before refactoring the spawn.

### The wait_lease cancel-before-start question

We worried about `wait_lease` leaking if a resolver is cancelled
before its first checkpoint, because the existing
`resolver_with_scope` wrapper only discards `survival_lease` in its
`finally`. In anyio, `start_soon` actually runs the task body — the
first checkpoint inside `resolver()` raises `CancelledError`, and the
existing `try/except/finally` machinery runs. `wait_lease` is
rebound to `current_lease` immediately and passed into
`wait_or_forward` / `wait`, which take ownership; if they raise
`CancelledError` without consuming it, the lease leaks. This is a
**pre-existing concern on the `_on_unpull` cancel path**, not
introduced by routing the devaluator through the same primitive.
Track it as a follow-up rather than blocking this fix.

### Why this is safe

- "Spawning earlier" means we eagerly begin walking the
  `local_future`'s forward chain and registering a wait on it.
  Preemptive resolution is explicitly allowed by the protocol
  (`PullMessage` docstring: "the remote side is free to preemptively
  resolve a future without waiting for a pull").
- The local future's *computation* runs independently of whether a
  resolver has parked on its box, so spawning early doesn't start any
  work that wouldn't otherwise have happened. Per-call cost is one
  task slot + the two cloned leases until either the resolver emits
  `ResolveMessage` or `UnpullMessage` cancels it via the new
  `_decrement_resolve_obligation_count` path.
- Pipelining chains that don't end up needing the value still tear
  down correctly: the peer's `UnpullMessage` drives ROC to 0, which
  routes through `_decrement_resolve_obligation_count` and cancels
  the resolver — exactly as `_on_unpull` does today.

### Tests to add / update

- Promote the existing red test
  (`TestOutboundAwaitFutureExprOnLocalFutureExport.test_devaluator_bump_alone_must_produce_resolve`)
  to a regular passing test — remove the `xfail` marker.
- Add a positive test for the "second devaluator bump in between
  Unpull and Pull" race: two back-to-back outbound dispatches with a
  controlled delay (or a manually-injected Unpull) between them,
  asserting both resolves arrive.
- Add a test for the devaluator rollback path: force a devaluator
  failure *after* `_bump_await_future_obligation` has incremented
  (e.g. a malformed downstream `IRCallArg`), and assert the spawned
  resolver is cancelled and the connection drains cleanly. With the
  unified primitive this is just verifying that the
  `_decrement_resolve_obligation_count(entry)` in the rollback path
  cancels the resolver.
- Cancellation path: existing `_on_unpull` tests already cover
  scope-cancel; add a variant where the resolver was spawned by the
  devaluator (not `_on_pull`) and an `UnpullMessage` arrives before
  any `PullMessage`.
- Drain test: outbound `AwaitFutureExpr` referring to a local future
  that never receives a Pull. The CM exit should drain cleanly once
  the eager resolver emits its `ResolveMessage` (or the peer's
  `UnpullMessage` cancels it).

### Documentation updates

- `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` — the "Resolve-obligation
  accounting (§2h)" subsection should describe the unified
  `_increment_resolve_obligation_count` / `_decrement_…` primitives,
  the invariant `resolve_task_cancel_scope is not None ⟺ a resolver
  task is alive`, and the fact that `_on_pull`, the devaluator, and
  `_on_unpull` all route through these.
- `lowering_message_handlers.py::_on_pull` docstring — strip the
  spawn description; refer to the primitive.
- `lowering_message_handlers.py::_on_unpull` docstring — refer to
  the primitive; keep the tolerance-rules block (still applies via
  the clamp inside `_decrement_…`).
- `lowering_devaluate.py::_bump_await_future_obligation` docstring —
  add a paragraph noting the spawn-on-0→1 obligation symmetric with
  `_on_pull`, routed through `_increment_resolve_obligation_count`.
- `FutureExportEntry.resolve_task_cancel_scope` docstring — restate
  the invariant: "None iff no resolver task is currently running for
  this export. Maintained by
  `_increment_resolve_obligation_count` (spawn on 0→1) and
  `_decrement_resolve_obligation_count` (cancel on N→0); the
  Resolve/Reject emit path zeroes the count via
  `_clear_resolve_obligation_on_send` without going through
  `_decrement_…`."
- The two helpers' own docstrings — say plainly that they are the
  *only* mutators of `resolve_obligation_count`, with the noted
  exception of `_clear_resolve_obligation_on_send`.

## Implementation steps

- [x] Add `grant_export_id: int | None` to `FutureExportEntry` and
      populate at the registration sites (`_register_future_export` via
      `_on_push`; lazily via `_pair_grant_with_future_export` in the
      devaluator's `UnwrapForPipelining` walk).
- [x] Factor the resolver spawn body out of `_on_pull` into
      `spawn_resolver_for_future_export` (module-level in
      `lowering_message_handlers.py`), invoked via
      `LeasedCapRPCConnection._ensure_resolver_running_for`. Uses the
      paired `grant_export_id` to find the grant entry.
- [x] Replace `_bump_resolve_obligation_count` /
      `_undo_bump_resolve_obligation_count` with
      `_increment_resolve_obligation_count(..., *, count=1)` /
      `_decrement_resolve_obligation_count(..., *, count=1)`. Resolver
      spawn (on 0 → positive) and cancel (on positive → 0) folded in.
      Over-decrement clamp moved into `_decrement_…` via
      `phase1_protocol_violation`.
- [x] Update `_on_pull` to call `_increment_resolve_obligation_count`
      and drop the explicit spawn / accumulation gate.
- [x] Update `_on_unpull` to call
      `_decrement_resolve_obligation_count(entry, count=msg.refcount)`
      and drop the inline decrement/cancel body.
- [x] Update `_bump_await_future_obligation` (lowering_devaluate.py)
      to call `_increment_resolve_obligation_count`; its `ctx.undo_log`
      entry calls `_decrement_resolve_obligation_count(entry)` — no
      spawn-awareness at the call site.
- [x] Verify `_zero_future_export_obligation` still works as a direct
      zeroing path (caller is the resolver itself, mid-emit) — asymmetry
      documented in `LOCAL_CODE_OVERVIEW.md` and on the primitives'
      docstrings.
- [x] Remove the `xfail` marker on
      `test_devaluator_bump_alone_must_produce_resolve` and verify it
      passes. (Test also restructured to raise a sentinel exception on
      success so the connection's CM drain-wait doesn't hang on the
      orphaned grant export.)
- [x] Add a positive test for cancellation of a devaluator-spawned
      resolver via inbound `UnpullMessage`
      (`test_devaluator_spawned_resolver_cancelled_by_unpull`).
- [x] Update `LOCAL_CODE_OVERVIEW.md` and the relevant docstrings:
      `FutureExportEntry` (prominent invariant block), the new
      `_increment_…` / `_decrement_…` / `_ensure_resolver_running_for`
      methods, the new `spawn_resolver_for_future_export` helper, the
      updated `_on_pull` / `_on_unpull` / `_bump_await_future_obligation`
      docstrings, and the new `_pair_grant_with_future_export` helper.
- [x] Re-run the full v2 test suite (`uv run pytest -n auto`) — all 2397
      tests pass.
- [ ] Re-run the workstation-demo repro to confirm the original
      uppercase-invoke hang is fixed; clean up trace/diagnostic
      instrumentation (env-gated `FXTR_REPRO_TRACE` prints in
      `state.py`, `lowering.py`, `lowering_message_handlers.py`).
- [ ] Update `v2/CODE_OVERVIEW.md` if any of the renames/structural
      changes warrant a mention there.

## Follow-ups (not blocking)

- `wait_lease` ownership on cancel-before-first-resume: the
  `resolver` body assigns `wait_lease` to `current_lease` and passes
  it into `wait_or_forward` / `wait`; if those raise `CancelledError`
  without consuming the lease, it leaks. Pre-existing on the
  `_on_unpull` path; worth tightening in a separate change by
  giving `resolver_with_scope` ownership of `wait_lease` in its
  `finally` (discard if not already consumed).

- [x] **`wait_for` slot of `HandleRequestExpr` has the same bug.**
  The protocol contract documented on
  `FutureExportEntry.resolve_obligation_count` and
  `FutureImportEntry.resolve_obligation_refcount` is "every
  `AwaitFutureExpr` referencing the future creates an obligation,
  symmetrically on both sides." The receive side honors this
  uniformly via `_phase1_walk` → `_phase1_bump_await_future_obligation`.
  The send side honors it in the `UnwrapForPipelining` branch of
  `_devaluate_walk` (the fix above) but **not** in the `wait_for`
  loop of `_build_handle_request_expr` (lowering_devaluate.py:310-319),
  where each devaluated `AwaitFutureExpr` is appended to
  `wait_for_exprs` without any call to `_pair_grant_with_future_export`
  or `_bump_await_future_obligation`. Resulting deadlock shape is
  identical to the original extra6 bug: peer's
  `resolve_obligation_refcount` for our future is bumped on receipt
  of the inbound `AwaitFutureExpr`, peer's `RemoteIRFuture.wait`
  skips its Pull, our `resolve_obligation_count` is never bumped, no
  resolver spawns, peer's `_eval_wait_for` blocks forever.

  Fix: in the `wait_for` loop, after the two `_devaluate_walk` calls,
  invoke `_pair_grant_with_future_export(conn, fut_expr, uwlease_expr,
  ctx)` and `_bump_await_future_obligation(conn, fut_expr, ctx)` —
  exactly the same three lines used in the `UnwrapForPipelining`
  branch. The unified `_increment_resolve_obligation_count` primitive
  then handles spawn-on-0→1; the `ctx.undo_log` rollback path covers
  failures mid-walk via `_decrement_resolve_obligation_count`. Add a
  red test that exercises a local future passed through
  `wait_for=` (rather than the dispatch's pipelined lease slot), and
  verify the protocol exchange drains cleanly the same way the
  existing two tests do.
