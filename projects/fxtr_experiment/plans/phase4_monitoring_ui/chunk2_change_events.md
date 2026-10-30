# Chunk 2 — the change-event stream

**Status: complete.**

## Scope

- **Session-side emission seam** (`state_store.py`): a plain in-process
  listener hook (no service machinery in the store) that mutating
  bodies call with the hint vocabulary of `notes/monitoring_ui.md`:
  `grids` (grid create/delete), `cells` + grid path (writes, clears,
  status flips, generation turnover, stamp-clearing walks — one hint
  per *grid touched*, the walk already knows them), `intent`
  (target/sourcing CRUD), `bindings` (emitted capability-side, below).
  Every mutating body grows an **"Events:"** docstring line beside its
  "Marks:" line — the greppable audit trail. **Hints fire only after
  commit** (the body accumulates them; the session flushes
  post-transaction): refetch-to-reconcile is only sound if hint
  delivery happens-after the change is readable — a pre-commit hint
  can trigger a refetch that reads the old state, and no further hint
  ever comes for that change, leaving the subscriber permanently
  stale. (A rolled-back transaction's accumulated hints are dropped
  too, though a leaked one would only cost a wasted refetch.)
- **The capability-side hub** (`user_service.py`): a
  `SubscriberHub`-based adapter owning the service-facing side —
  subscribes to the session hook at mount, **coalesces** within a short
  window (duplicate keys merged; ordering across keys not promised),
  and broadcasts wire dicts. `bindings` hints emitted here (the
  registry is capability-side state).
- **`subscribe(subscriber) -> ServiceRef[UnsubscribeHandle]`** on
  `UserCapabilityProto`, following the fxtr2 `EventSource` contract
  (`fxtr2.services.subscription_types`) so workstation inboxes attach
  via the standard `subscribe_to` idiom; `Workflow` passthrough.
  Subscriptions are ephemeral — nothing durable, nothing replayed; the
  hint-only vocabulary makes refetch-to-reconcile safe by construction
  (documented on the method).

## Tests

- Emission per mutation kind: one scenario arc asserting the expected
  hint set for writes / clear / delete_grid / delete_cell / target and
  sourcing CRUD / a demand that runs a task (status flips + result
  visibility), including hints for *task-driven* mutations (the
  executor path flows through the session).
- Coalescing: the window swap/encode logic unit-tested synchronously
  (lossless across flush boundaries, deduped, deterministic encoding,
  no empty publishes) + an e2e burst asserting per-batch dedup and the
  exact hint union. Batch *counts* are deliberately unasserted — the
  window length is delivery policy, not contract (at-least-once makes
  over-delivery always safe), and a count bound is a wall-clock bet
  (the first version flaked under a loaded `-n auto` run).
- No escape on refusal: a refused mutation (e.g. `clear_cells` while
  running) emits nothing.
- Subscriber lifecycle: unsubscribe stops delivery; a subscriber whose
  `on_event` raises is reaped (the hub's crash backstop) without
  affecting other subscribers or the mutation path.
- RPC smoke: subscribe over the wire, receive hints, unsubscribe.
- Pyright clean.

Notes/log:

- **Emission mechanism: ambient contextvar collector, not signature
  plumbing** (settled with Daniel mid-chunk). The new leaf module
  `change_hints.py` exposes `note_change(kind, path)` writing into a
  ContextVar'd mutable set and `collect_hints()` installing a fresh one;
  `WorkflowSession._run` (and `_record_generation_locked`'s final
  transaction, the one mutating path bypassing `_run`) install + flush
  to the listener on clean return — post-commit ordering and
  rollback-drops-hints fall out structurally. anyio propagates context
  into `to_thread` workers (verified), so sync bodies and the walks
  note into the same set with **zero signature changes** — which is
  what makes per-grid precision cheap: `mark_grounding` (all four
  clear branches) and `_propagate_clears` note `("cells", grid)` for
  every grid whose stamps they actually clear.
- **Wire payload = batches.** The hub's own "coalesce" delivery mode
  keeps only the latest pending event (would drop distinct hint kinds),
  so the capability-side `_EventBroadcaster` window-dedupes (0.05s
  constant) and publishes ONE list-of-hint-dicts per flush over a
  queue-mode `SubscriberHub`, `origin_scope=None` (no ids in payloads).
  Lives in the capability's new `_fxtr_mount_lifespan`, wired to
  `WorkflowSession.set_event_listener` (one listener, install-over-live
  raises) + `BindingRegistry.set_change_listener` (the `bindings` hint;
  auto-release paths emit too since release goes through the registry).
- **Emit-on-change for sourcing upserts** (found by the silence test):
  a same-value write's sourcing re-upsert re-pins an identical
  fingerprint — displayed state unchanged — so `_upsert_user_sourcing`
  notes `intent` only when the pin actually changed. Target
  add/remove and revoke similarly gate on actual row changes.
- `apply_import_body` emits the coarse `grids` + `intent` pair
  (per-grid precision is pointless for a wholesale replace).
- `record_failure` and `put_checkpoint` DO emit (mid-run failure rows /
  checkpoint entries are inspectable via `inspect_generation`);
  `record_result` / observation / assertion / request-edge inserts do
  not (invisible until the status flip — though a request-edge insert's
  grounding mark may emit walk hints, which is correct: wanted verdicts
  are displayed state).
- **The RPC smoke's teardown hang** (the one failure open at the
  context handoff) was the subscription cross-pin cycle, not a
  delivery problem: the client's rpc session scope hosted the
  subscriber mount (pinned by the server hub's sub_scope) *and* held
  the unsubscribe handle, and gracefully draining such a scope parks
  forever — the exact hazard `fxtr_workstation/callback_impl.py`'s
  module docstring documents. Fix: the test unsubscribes explicitly
  before the client block exits (the other sanctioned client pattern
  is holding the handle in a shorter-lived scope so handle-drop
  teardown runs first); the contract is now stated on `subscribe`'s
  Returns section in `user_api.py`.
