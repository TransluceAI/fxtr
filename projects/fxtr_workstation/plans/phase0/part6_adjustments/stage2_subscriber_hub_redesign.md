# Stage 2 — SubscriberHub redesign: seeded delivery, event-borne residency, non-pinning subscriptions

Redesign of `v2/fxtr2/services/subscription_util.py`'s `SubscriberHub`
(plus small consumer migrations), resolving the two questions scoped in
`stage1post1_demo_refactor_scoping.md` and the teardown-topology
discussion that followed. Design rationale and the discussion record
live in that scoping doc; this is the implementation spec.

The wire protocols (`Subscriber` / `UnsubscribeHandle` / `EventSource`
in `subscription_types.py`) are **unchanged**. Everything here is
hub-internal plus hub-API surface, so consumers migrate mechanically.

Builds on stage 12 (`v2/plans/phase3/stage12_better_scope/`): explicit-
lease scope entry, `clone_authorization`, base-vs-override
authorization, and the drained-but-open dispatch story are all
load-bearing below.

## Goals

1. **Seeded subscriptions** (replaces `deliver_current`): a subscriber
   can be seeded with caller-chosen initial values (e.g. a state
   snapshot) delivered before the live stream.
2. **Events carry their own residency**: the hub stops requiring
   published payload entities to live in a hub-owned scope. An event
   pins its *origin* scope (via a captured lease) only until it is
   delivered or superseded, then drops it. The hub no longer takes a
   `scope` constructor arg at all.
3. **Subscriptions never pin the source's mount scope**: all
   per-subscription scopes are isolated (not descendants of any
   source scope), so a source whose real holders are gone can drain
   and tear down even with live subscriptions outstanding — enabling
   clean source-initiated shutdown.
4. **Handle-drop is a teardown signal**: a peer that releases its
   `UnsubscribeHandle` (politely or by dying) tears the subscription
   down without waiting for a failed delivery. Contract: *hold the
   handle to keep your subscription; hold the source ref to keep the
   source.* Neither implies the other.

## Design

### D1 — Event residency: origin leases + per-delivery scopes

`publish` stays sync but captures provenance:

```python
hub.publish(value)                          # origin = active scope
hub.publish(value, origin_scope=scope)      # explicit origin
hub.publish(value, origin_scope=None)       # no origin: payload has no entity ids
```

- With an origin, the hub takes `origin_scope.clone_authorization()`
  (sync; requires the publisher's context authorized for it — true in
  any dispatch publishing from its own call scope). Each live
  subscription's pending entry holds the value plus **its own clone**
  of that lease.
- Delivery opens a transient per-event scope via
  `open_with_explicit_leases(parent_leases={sub_scope: <auth clone>,
  origin_scope: <the entry's lease>})` — explicit-lease entry is the
  one path allowed to derive from a draining origin, and entry
  *consumes* the lease, so the origin claim's lifetime becomes the
  delivery scope's lifetime with no extra bookkeeping. The subscriber
  proxy is resolved into that scope, `on_event` dispatched, scope
  closed. (This is the prototype's per-emit both-parents notify scope,
  made sound by the captured lease.)
  (**Note:** it would also work to re-authorize the scope with `authorize`
  and then use the normal open constructor.)
- Coalesce mode discards the superseded entry's lease on overwrite;
  queue mode holds one lease per pending value; every worker exit path
  discards whatever is pending. "Events are held only until delivered,
  then dropped" is structural.
- `origin_scope=None` publishes with no lease; the delivery scope has
  only `sub_scope` as parent. For payloads carrying no `FxtrID`s
  (notifications, shutdown messages). Publishing an id-bearing payload
  without an origin is a caller bug that surfaces as a delivery
  encode error.

Semantics to document: a pending event pins its origin scope
(drained-but-open) until delivered or superseded. A slow queue-mode
subscriber therefore holds old origin scopes open — correct, but worth
a docstring warning. Coalesce mode self-bounds.

Cost note: one scope open + proxy resolve per delivery (the prototype
did exactly this per emit). Fine at current rates; if a hot source
shows up, per-(subscription, origin) proxy caching is the future
optimization — do not build it now.

### D2 — Seed (replaces `deliver_current`)

```python
await hub.subscribe(subscriber, source_scope,
                    seed=[snap],            # delivered first, in order
                    seed_origin=run_scope)  # like publish's origin_scope
```

- Seed values are pushed into the fresh subscription's strategy before
  the entry is appended (same sync window `deliver_current` used), so
  they precede any concurrent publish.
- `seed_origin` works exactly like `publish`'s `origin_scope`
  (default: active scope; `None` for entity-free seeds). The hub takes
  one `clone_authorization()` and clones it per seed item. A caller
  whose origin scope is draining (e.g. a retained previous-run scope)
  wraps the call: `with run_scope.authorize(lease.clone()): await
  hub.subscribe(..., seed_origin=run_scope)`.
- **Recommendation, not contract**: a caller wanting a gapless
  snapshot→stream handoff must not await between computing the seed
  and calling `subscribe`. Awaiting is allowed if the caller knows
  what it's doing; events landing in the gap are simply in neither.
- `deliver_current` and the hub's retained `_current` value are
  **deleted**. The agent MCP keeps its own latest head and passes
  `seed=[head]` (it already stores heads in its impl scope —
  `seed_origin` points there).

### D3 — Subscription topology: everything isolated

Per subscription there are now two small scopes and two tasks, none of
them descendants of any source scope:

- **`sub_scope`** (worker task): isolated (`include=[GLOBAL_SCOPE]`
  for type-descriptor reachability — verify what the minimum is).
  Holds the subscriber bridge (independent lease on the subscriber
  ref's chain token — the standing delivery route) and parents every
  per-delivery scope. The worker loop is unchanged in spirit: take
  pending → deliver → park on wake.
- **`hold_scope`** (holder task): isolated; hosts the mounted
  `UnsubscribeHandle` impl and nothing else. Its lifetime *is* the
  handle's external-holder lifetime — see D4.

Because neither scope includes the source's mount scope, and the
subscribe call scope drains promptly (its only job is the handoff),
**live subscriptions leave the source's mount token unpinned**. What
keeps the source alive is holders of the *source ref* — nothing else.

Verified enabler (`rpc/lowering_devaluate.py`): a chain export's
`backing_lease` is on the exported handle's **own token**
(`_ensure_token_exported(conn, handle.token, ...)`), so a peer holding
the returned handle pins `hold_scope` + the connection, not the
dispatch call scope it rode out through. Releases propagate as
`DiscardMessage`s, source-side revokes as `BreakMessage`s.

### D4 — Handle holder: natural drain as the drop signal

Chosen shape (**variant B — natural drain**): the holder task exits the
scope CM immediately after setup, so `aclose` itself drops the base
authorization and parks in the token-drain wait. The scope is then
draining-but-open for exactly as long as the peer holds the handle;
inbound `unsubscribe` dispatches still land (wire-lease derived path).
The drain completing *is* the "handle dropped" signal — straight-line
code after the block, no lifespan hook, no lowlevel calls:

```python
async def _handle_holder(self, entry, source_scope, *, task_status):
    with anyio.CancelScope() as entry.holder_cancel:
        async with FxtrScope(isolated=True, ...) as hold_scope:
            ref = await hold_scope.mount(_HubUnsubscribeHandleImpl(entry))
            # Bridge out HERE, while hold_scope's base is still live —
            # subscribe() no longer does the bridging.
            await source_scope.bridge_service_from_scope(
                ref, hold_scope, keepalive_via="independent_lease")
            task_status.started(ref)
        # CM body ends at once: aclose drops the base and parks in the
        # token-drain wait. Unblocks when the peer's last handle lease
        # is discarded (DiscardMessage) or the token is revoked.
    entry.request_unsubscribe()   # idempotent: drop-fallback AND post-cancel
```

Rejected alternative for the record (**variant A — manual discard**):
keep the CM body open, call `hold_scope.discard_base_authorization()`
after the bridge-out, park on an event, and have the handle impl
override `_fxtr_mount_lifespan` so its teardown (which runs after the
token drains) fires `request_unsubscribe()`. Functionally equivalent;
loses to B because the fallback is indirected through a lifespan hook,
it uses a lowlevel API mid-CM whose `aclose` interaction needs
auditing, and it needs an extra wake event. B *is* "the CM starts
draining when it's safe to do so" — the signal that it's safe is
simply that setup + bridge-out are done.

Teardown paths:

- **Explicit `unsubscribe`**: flag + wake as today; the worker tears
  down `sub_scope` (forcefully, as today). The holder is **not**
  cancelled — the peer releases the handle after its `unsubscribe`
  returns (existing contract), the holder's drain-wait then completes
  naturally and the trailing `request_unsubscribe()` is a no-op. This
  structurally removes the old reply race, so **`unsubscribe_grace` is
  deleted** (the unsubscribe reply rides `hold_scope`, which the peer
  still pins while awaiting it).
- **Handle dropped / peer died**: drain completes (discard or break) →
  trailing `request_unsubscribe()` → worker tears down. No more
  waiting for the next delivery to fail; the crash backstop on
  `on_event` errors stays as the residual net.
- **Hub teardown / crash backstop**: worker cancels
  `entry.holder_cancel` → CM exception path → `force_close` →
  `BreakMessage` → peer's handle reads as gone (same durable signal as
  today).

### D5 — Source-initiated close: client-level message, two hub affordances

No reserved `on_source_closed` protocol method (considered; rejected
for now — a domain-level message keeps close out of the framework,
lets it carry data, and needs no Subscriber-protocol change). Instead,
a source that wants polite shutdown publishes its own close message
and then closes the hub:

```python
# e.g. in the source impl's _fxtr_mount_lifespan teardown:
hub.publish({"kind": "scheduler_closing"}, origin_scope=None)
await hub.aclose()        # flush, then tear everything down
```

- `hub.aclose()`: **new.** Wake all workers, wait (unbounded) for each
  live subscription's pending values to flush (strategy empty *and*
  the in-flight `on_event` returned), then cancel every holder +
  worker (forceful teardown as usual). Also the thing the owning
  lifespan calls in its teardown / `finally` instead of raw TG
  cancellation. There is deliberately **no flush timeout**: a bounded
  / forceful close is just "cancel the owning task" — cancellation
  already force-closes everything. `publish` / `subscribe` raise once
  `aclose` has begun.
- This works from an *unauthorized* post-drain mount-lifespan teardown
  precisely because of D1's `origin_scope=None` publish (no lease
  needed) and because deliveries run through `sub_scope`s, which are
  isolated and still healthy.
- End-to-end walkthrough (the motivating scenario): A holds the source
  ref, B is subscribed, A drops the ref → mount token drains (D3: B
  pins nothing on it) → source lifespan teardown publishes the close
  message + `hub.aclose()` → B receives it, releases its handle and
  subscriber → holder drains, everything unwinds. No mutual wait.
- Sources that *don't* send a close message degrade to today's
  behavior (subscribers find out via broken handles / delivery
  errors). Fine.

### Resulting public surface (sketch)

```python
hub = SubscriberHub[T](task_group=tg, mode="coalesce" | "queue")
hub.publish(value, *, origin_scope=...)              # sync
await hub.subscribe(subscriber, source_scope,
                    *, seed=None, seed_origin=...)   # -> handle ref
await hub.aclose()                                   # unbounded flush, then teardown
hub.live_subscription_count                          # unchanged
```

Removed: `scope=` ctor arg, `mode` retained-current machinery
(`_has_current` / `_current`), `deliver_current`, `unsubscribe_grace`.
`_DeliveryStrategy` push/take now move `(value, lease | None)` pairs;
the strategies own discarding superseded/undelivered leases.

## Consumer migrations

All mechanical; no proto/wire changes anywhere.

- **`basic_dynamic_dispatch` / `nix_configurable_dispatch`**
  (fxtr_workstation): ctor loses `scope=`;
  `trigger_reload()` becomes `hub.publish(
  ReloadEverythingNotification().store(self._scope),
  origin_scope=self._scope)` — they keep their harness `impl_scope`
  for the notification entity; the hub just no longer requires it.
  Harness teardown calls `hub.aclose()` **bounded at the call site**
  (`anyio.move_on_after(SUBSCRIPTION_FLUSH_TIMEOUT_SECONDS)` around
  the flush, then an unconditional `impl_tg.cancel_scope.cancel()` as
  the forceful backstop — a no-op when the flush completed; the
  timeout only cancels the `aclose` *call*, so the TG cancel is what
  actually force-closes a wedged subscription).
- **Agent MCP plugin**: `deliver_current=True` → keep latest head in
  the impl (it already does), `subscribe(..., seed=[head],
  seed_origin=impl_scope)`; `update_head` → `hub.publish(head,
  origin_scope=impl_scope)` (heads are stored there today).
- **Workstation inbox / parker** (`callback_impl.py`): **no changes
  required.** The parker already bridges + holds the handle
  (= subscription keepalive ✓) and force-closes its subscription scope
  on inbox close (→ DiscardMessage → holder drain → source-side
  cleanup even when the best-effort `unsubscribe` times out — the old
  zombie window closes for free). Optional future polish, not this
  stage: inbox-side handling that auto-closes a callback record when
  its source goes away.
- **Sandbox bridge**: nothing — protocols unchanged.

## Implementation chunks

### Chunk 1 — hub core redesign (`v2/fxtr2/services/subscription_util.py`)

- [x] D1: origin capture in `publish`, `(value, lease)` strategies,
      per-delivery scopes via `open_with_explicit_leases`,
      per-delivery proxy resolve; delete the hub `scope` arg and the
      retained-current state.
- [x] D2: `seed` / `seed_origin` on `subscribe`; delete
      `deliver_current`.
- [x] D3/D4: isolated `sub_scope`; holder task + `hold_scope` with
      natural-drain drop detection; bridge-out moves into the holder;
      delete `unsubscribe_grace`.
- [x] D5: `hub.aclose()` (unbounded flush; publish/subscribe raise
      once close has begun).
- [x] Rewrite `tests/test_services/test_subscription_util.py` for the
      new model. New coverage beyond the ported cases:
      - origin-lease lifecycle: payload entities in a since-drained
        call scope deliver correctly (no copies); superseded coalesce
        value releases its origin promptly; worker teardown releases
        pending origins.
      - seeds: order, seed-then-stream interleave with concurrent
        publish, `seed_origin` on a draining scope via `authorize`,
        entity-free seed with `seed_origin=None`.
      - handle-drop fallback: peer discards handle without
        unsubscribing → subscription tears down promptly (no delivery
        needed); explicit unsubscribe → holder completes without
        cancellation and the reply is never raced.
      - source-teardown: publish(origin=None) + `aclose()` flushes a
        close message to live subscribers before force-close;
        `aclose` with a wedged subscriber unwinds cleanly when the
        owning task is cancelled (cancellation force-closes).
      - non-pinning: a source scope with live subscriptions drains
        once its own holders are gone (bounded-timeout test — this is
        the headline property).
- [x] Update `v2/fxtr2/services/LOCAL_CODE_OVERVIEW.md` and
      `v2/CODE_OVERVIEW.md`.

### Chunk 2 — consumer migrations

- [x] Dispatchers (both) + their tests.
- [x] Agent MCP impl + `tests/test_agent_mcp_impl.py`
      (deliver-current suites become seed suites) +
      `tests/test_callback_e2e.py` re-run.
- [x] Workstation: no source changes (only the `_feed_harness` test
      fixture's hub ctor); `test_sessions_5d.py` + dispatch suites +
      full workstation suite green; added
      `test_close_callback_handle_drop_alone_tears_down_source_side`
      (unsubscribe timeout forced to 0 → teardown via handle drop
      alone, crossing the real RPC chain).
- [x] Update `projects/fxtr_workstation/CODE_OVERVIEW.md` +
      `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` where
      hub behavior is described.

## Risks / things to verify during implementation

- **Prompt discard propagation**: a peer-side *force-closed* scope
  must emit DiscardMessages (or break-equivalents) for its imports
  promptly, or handle-drop detection degrades to
  connection-teardown-time. The lowering machinery is built for this
  (`_subscribe_backing_lease_break`, discard-on-zero `live_leases`);
  verify with a cross-connection test, including the workstation
  parker's force-close path.
- **Drained-origin delivery**: a retained lease on a drained call
  scope must keep its arena loadable for delivery-scope entry +
  dispatch encode (the drained-but-alive contract; load-bearing for
  zero-copy publish). Direct test in chunk 1.
- **Holder drain-wait vs. force_close**: cancelling the holder while
  it is parked inside `aclose`'s drain wait must unwind cleanly
  through the CM's exception path (and an external `force_close`
  racing the parked `aclose` must not double-close). Audit + test.
- **Isolated-scope descriptor reachability**: confirm what
  `include=` an isolated `sub_scope`/`hold_scope` needs for the
  unsubscribe-handle type descriptor and proxy resolution
  (`GLOBAL_SCOPE` expected).
- **`subscribe` failure surfacing**: holder or worker failing before
  `started()` must still propagate cleanly to the subscriber's
  `subscribe` call (now two `task_group.start`s instead of one).

## Decisions (Daniel, 2026-06-10)

- Seed is a plain sequence; gapless handoff is a *recommendation*
  (no await between snapshot and subscribe), not a contract.
- `deliver_current` is deleted, not deprecated — callers own their
  latest-value retention.
- Close messages are **client-level** (domain payloads), not a
  reserved `Subscriber` method; the hub provides `origin_scope=None`
  publish + flushing `aclose` to make that pattern work from
  unauthorized teardown.
- Subscriptions deliberately do **not** keep the source alive
  ("fully embrace"): source keepalive = hold the source ref;
  subscription keepalive = hold the handle. The old semantics' use
  case (subscriber wants source pinned) is served by also holding
  the source ref.
- Handle holder uses natural drain (variant B), not manual
  `discard_base_authorization` (variant A) — see D4 for both
  sketches and the rationale.
- **Broken origin lease at delivery time kills the subscription**
  (routed into the crash backstop), rather than silently skipping the
  event: skipping could deliver later events out of expected order and
  leave the subscriber in an invalid state. A subscriber can always
  resubscribe for a fresh view.
- `publish` / `subscribe` **raise** once `aclose` has begun (the close
  pattern is publish-then-aclose, so either afterwards is a caller
  bug).
- `aclose`'s flush is **unbounded** — no `flush_timeout` param. A
  bounded/forceful close is expressed by cancelling the owning task;
  cancellation force-closes every subscription already.
- A peer that unsubscribes politely but never releases its handle
  leaves that subscription's holder task parked until `hub.aclose()`.
  Accepted: by then the worker has force-closed `sub_scope` (releasing
  the subscriber bridge — the only client-resource pin), so the parked
  holder retains only the empty `hold_scope` with the handle impl.
- The dispatcher mount harnesses bound their teardown flush
  **at the call site** (`move_on_after` around `aclose()` + an
  unconditional `impl_tg.cancel_scope.cancel()` backstop) rather than
  baking a timeout into the hub — mechanism (unbounded flush) stays in
  the hub, policy (how long shutdown may wait on slow peers) at the
  owner. Decided after observing the unbounded harness flush turns a
  previously-instant teardown into one that trusts in-flight
  deliveries to complete or error.

## Implementation notes (running log)

- **Per-delivery scope mechanics**: the strategies move
  `(value, origin_scope, lease)` triples (not `(value, lease)` pairs —
  `open_with_explicit_leases` keys parent leases by the `FxtrScope`
  object, so the scope rides along). Delivery scopes are
  `isolated=True, include=[sub_scope (+ origin)]`; `include=
  [GLOBAL_SCOPE]` was confirmed sufficient for `sub_scope` /
  `hold_scope` (descriptor + proxy resolution all pass).
- **Idempotent re-`unsubscribe` is now genuinely a no-op** while the
  peer still holds the handle (the old behavior surfaced a transport
  error once the subscription scope was torn down): the handle lives
  in `hold_scope`, which stays draining-but-open for as long as the
  peer pins it. After `hub.aclose()` (holder force-closed) it errors
  as before.
- **Holder-cancel race latch**: `cancel_holder()` can fire before the
  holder task has assigned its `CancelScope` (e.g. crash backstop
  during the subscribe window) — latched via
  `holder_cancel_requested`, checked by the holder right after
  assignment.
- **Wedged-subscriber test caveat** (`test_aclose_with_wedged_
  subscriber_unwinds_on_cancel`): a permanently-wedged `on_event`
  dispatch pins the *subscriber's own mount scope* (the dispatch task
  holds a per-call lease on it), so the test must release the orphaned
  dispatch after the hub unwinds or the mount scope CM hangs at exit —
  that pin is the subscriber impl's, not the hub's.
- **Agent-plugin full suite under `-n auto` is load-sensitive**: two
  full parallel runs each had one/few failures in heavy sandbox/nix
  e2e tests (`test_mcp_in_memory.py::test_in_memory_e2e_nix_spawn`
  TimeoutError; fixture errors in `test_callback_e2e.py` /
  `test_event_log_chain.py`), every one passing serially. One run also
  hung at pytest shutdown after printing its summary (not reproduced).
  Believed pre-existing load sensitivity (cf. the stage-1 note about
  the load-dependent supervisor flake), not a redesign regression —
  but worth watching.
- Chunks 1 and 2 went into **one jj change**: chunk 1 alone leaves the
  two dispatcher ctor calls broken (pyright pre-commit fails), so
  there was no green intermediate commit point.
