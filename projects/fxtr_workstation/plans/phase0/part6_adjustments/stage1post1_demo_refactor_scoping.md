# Stage 1 post 1 — scoping: porting the scheduler prototype onto the subscriber system

Scoping doc, not a plan. Anchors the discussion of two design
questions that came out of trying to back-port the
`~/tmp/fxtr-demo-workdir-workflowtest` scheduler prototype (simple
workflow scheduler + task system, see `SCHEDULER_PROTOTYPE_STATUS.md`
in that workdir) into `examples/fxtr_workstation_demo/agent_workdir/`
after the stage-1 subscriber refactor:

1. **Replay/backlog doesn't fit `SubscriberHub`** — the hub's delivery
   mechanics may be too coupled; queue mode was meant for cases like
   this and has no consumer yet, so this is the moment to generalize
   it.
2. **A guest impl has no sanctioned scope/task group** to run a hub
   on — and the obvious candidate (the mount scope) is *draining* for
   essentially the service's whole life. Ties into the broader
   "mounted impls should get a supported interface to their mount
   scope / task group" redesign.

Everything else about the port is mechanical (entity types, task
packages, renderers, flake wiring, README) and is not covered here.

## RESOLUTION (2026-06-10)

Both questions are settled; the analysis below is kept as the record,
with outcome notes inline. The work splits into two stages (no
`stage1post1_demo_refactor_plan.md` will be written):

- **`stage2_subscriber_hub_redesign.md`** — Q1 grew beyond option (a)
  into a hub redesign: caller-supplied `seed` (with `deliver_current`
  deleted), **event-borne residency** (publish captures an
  origin-scope lease per event, deliveries run in transient
  per-event scopes parented on `{sub_scope, origin}`, the lease is
  consumed into the delivery scope — the hub loses its `scope`
  constructor arg entirely), **non-pinning subscriptions** (all
  per-subscription scopes isolated; the unsubscribe handle lives in a
  holding scope whose natural drain doubles as handle-drop
  detection), and a client-level close-message pattern
  (`publish(origin_scope=None)` + flushing `hub.aclose()`).
  Contract: *hold the source ref to keep the source; hold the handle
  to keep your subscription; neither implies the other.*
- **`stage3_demo_refactor.md`** — the actual port, on stage 2 plus
  the stage-12 scope redesign
  (`v2/plans/phase3/stage12_better_scope/new_scope_design.md`), which
  resolved Q2: guest impls get a blessed mount-time lifespan hook
  (`_fxtr_mount_lifespan`) that can own a task group, and per-call
  authorized mount-scope access (`_fxtr_get_mount_scope()`).
  Scheduler shape: queue-mode hub, `run_state` snapshot as the seed,
  fine-grained deltas on the live stream, retention = a retained
  lease on the previous run's call scope (zero copies), and a
  `scheduler_closing` close message.

Key fact verified for the teardown design
(`rpc/lowering_devaluate.py`): a chain export's backing lease is on
the exported handle's **own** token, not the dispatch call scope it
rode out through — so handle holders pin only the (isolated) holding
scope, and peer-side releases propagate as `DiscardMessage`s. This is
what lets subscriptions stop pinning the source's mount scope, which
in turn lets a source whose real holders are gone tear down and
notify its subscribers (the A/B mutual-wait scenario resolves
cleanly). The general "scope that ends when only internal machinery
pins it" primitive (Q2(B)(ii)/(iii)) is still future work, but is no
longer needed for any of this.

## Why the prototype's subscription design is dead

The prototype (built pre-refactor) used two patterns the refactor
deliberately removed:

- **`notify(payload) -> bool` as the reap signal.** Its
  `SimpleEventListenerProto` mirrored the deleted
  `EventListenerProto`. The workstation inbox's `notify` now returns
  `None` and is a **silent no-op when closed** — so the scheduler's
  `ok = bool(await proxy.notify(payload))` would read every delivery
  as "declined" (drop after first event), and fixing that still
  leaves no way to ever learn the inbox is gone (zombie
  subscriptions delivering into the void).
- **The parked subscribe.** `subscribe(listener)` deliberately never
  returned — parking the dispatch was the only way to keep the
  listener ref alive (no guest scope to bridge it into; see
  `PROTOTYPE_FOLLOWUPS.md` item 2). Under the new system the frontend
  subscribes via `allocateInvoke(inbox.handle, "subscribe_to",
  [schedulerRef])`, and the workstation's parker task **awaits
  `source.subscribe(subscriber)`** before the `subscribe_to` dispatch
  returns (`callback_impl.py`, step 3 of the parker lifecycle). A
  subscribe that parks forever now hangs the whole
  `CallbackInvokeOp` → `subscribe_to` → parker chain, with the
  subscription never reaching "established".

So the ported scheduler must be a real `EventSource`: a re-declared
`subscribe(subscriber) -> ServiceRef[UnsubscribeHandle]` that
**returns promptly**, with per-subscription state living somewhere
that outlives the subscribe dispatch. That's exactly the shape
`SubscriberHub` packages — hence the two questions.

## What the ported scheduler needs

From `simple_scheduler` + the grid renderer's semantics:

- **R1 — prompt subscribe.** `subscribe` returns an
  `UnsubscribeHandle` ref while events keep flowing afterwards.
- **R2 — backlog for late subscribers.** A panel opened mid-run must
  render the whole grid, i.e. receive (the effect of) all events
  since the current run's `run_started`. (The old design did this
  with a replay buffer; "no run yet" after a finished run was an
  accepted artifact, not a goal.)
- **R3 — every event matters** *if* events stay fine-grained
  (task_started / task_result / …): the grid folds them, so coalesce
  mode would drop cells. (R3 evaporates if the event shape becomes a
  self-superseding snapshot — see Q1 option d.)
- **R4 — payload entity lifetime.** Events carry `FxtrID`s of step
  outputs stored in `run_batched`'s *dispatch* scope. Whatever
  delivers them later (worker loop, replay seed, deliver-current)
  needs those entities claimed somewhere that outlives that dispatch.
- **R5 — guest-side residency.** All of the above runs in a wasm
  guest, where the impl's only ambient anchor is the connect-dispatch
  scope it was mounted on.

## Q1 — generalizing `SubscriberHub` delivery

Today (`v2/fxtr2/services/subscription_util.py`): per-hub
`mode="coalesce" | "queue"` selects a private `_DeliveryStrategy`
(`push`/`take`, sync, never blocks); the only seeding affordance is
`deliver_current=True`, which pushes the single retained latest
value inside the no-await no-dup-no-miss window in `subscribe`.
Queue mode has no in-repo consumer. The scheduler needs "queue mode,
seeded with a backlog" — which doesn't exist.

### (a) Caller-supplied seed on `subscribe`

```python
await hub.subscribe(subscriber, source_scope, seed=replay_events)
# deliver_current=True becomes sugar for seed=[current] if any
```

`seed` items are `push`ed (in order) into the fresh subscription's
strategy inside the same no-await window. The *source* owns the
replay buffer and its horizon policy (clear at `run_started`); the
hub stays a pure fan-out.

- Pro: smallest hub change; replay policy stays with the one party
  that understands it; both modes get seeding for free.
- Con: every source that wants replay re-implements the buffer +
  horizon bookkeeping; the source must also solve R4 itself (keep
  seed payload entities claimed somewhere the hub's per-subscription
  scopes can reach — see Q2).
- Note: `deliver_current` + `seed` should be mutually exclusive (or
  `deliver_current` just deprecated into it).

### (b) Hub-owned retention strategy

Make retention a per-hub strategy alongside delivery, e.g.:

```python
SubscriberHub(scope=..., task_group=..., delivery="queue",
              retention="horizon")        # or "latest" / "none"
hub.publish(ev, new_horizon=(ev["kind"] == "run_started"))
```

`retention="latest"` is today's `_current`; `"horizon"` keeps every
publish since the last `new_horizon=True`. `subscribe(...,
deliver_retained=True)` seeds from whatever retention holds —
subsuming `deliver_current`.

- Pro: replay buffer + the seeding atomicity window live in the hub,
  next to the existing one; sources just tag horizon resets; any
  future stream-ish source gets replay for free.
- Con: couples the hub to a policy only one consumer needs so far
  (cuts against the "less coupled" instinct — though arguably
  retention-as-strategy *is* the decoupling); retained values raise
  the R4 lifetime question inside the hub: retained entity payloads
  must stay reachable from the hub's scope for as long as retention
  holds them, which makes the hub's scope requirements heavier (and
  interacts with Q2 — the hub has no scope of its own to claim
  things in; it can only document the requirement).
- The existing queue-bounds/backpressure TODO naturally extends to
  retention bounds.

### (c) Public delivery-strategy injection

Promote `_DeliveryStrategy` (push/take + optional `seed()`) to a
public protocol and accept a factory:

```python
SubscriberHub(scope=..., task_group=..., delivery=MyStrategyFactory)
```

`"coalesce"`/`"queue"` become the two built-in factories. (a) and/or
(b) can be expressed on top.

- Pro: maximal decoupling of the worker loop from delivery
  semantics; the mode enum stops being load-bearing.
- Con: by itself it doesn't give the scheduler replay (the strategy
  is per-subscription and created *by the hub*, so it can't see the
  backlog unless the factory closes over source state — at which
  point it's (a) with more indirection). Probably a refactor to do
  *alongside* (a)/(b), not an alternative to them.

### (d) Don't generalize: snapshot-shaped events at the source

Change the *event contract* instead: the scheduler publishes one
self-superseding **run-state snapshot** per change (run id, labels,
inputs, cell states, completion). `mode="coalesce"` +
`deliver_current=True` then already does everything: late
subscribers get current state as their first event (R2 ✓), coalesce
collapse is harmless (R3 moot), and a panel opened *after* a run
shows the completed grid (strictly better than the old "no run yet").
The grid renderer derives from the **last** inbox event instead of
folding the stream.

- Pro: zero fxtr2 change; deletes the replay machinery rather than
  porting it; the agent-MCP chain-head feed already validates this
  exact pattern (latest-value + deliver_current).
- Con: loses the fine-grained event stream as an API (any consumer
  that wanted transitions, not states, would need it back); payload
  grows to O(grid) per emit (trivial at demo scale); R4 remains —
  the retained snapshot's entity ids must stay claimed somewhere
  (one run's worth, at least), so Q2 doesn't go away, it just
  shrinks to "claim the current run's outputs".
- Also note: this is arguably the *right* shape for this UI
  regardless — the grid is a state view, not an event log.

**Interplay.** (d) minimizes what Q2 must provide (claims for one
run's entities); (a)/(b) with queue mode need entities claimed for
the whole replay horizon. If we expect more stream-shaped sources
soon, (b) [possibly via (c)] is the durable investment; if the
scheduler is the only customer, (d) + revisiting when a second
consumer appears is defensible.

**CURRENT LEANING:** (a) seems cleanest; it allows custom policies of what gets
sent to initial subscribers and allows either replaying multiple events or
coalescing state on the event source and sending a single coalesced snapshot
(like d) _once_ to reinitialize the subscriber, followed by streaming single-event
delivery as normal in either coalesced or queued modes (where queued mode could
send deltas). Sending a coalesced snapshot on connect can be the recommended flow.

**RESOLVED:** (a), as per the leaning — but the discussion went further:
rather than the source copying payloads into a persistent hub-owned
scope (the R4 framing all the options above assumed), events now carry
their own residency (publish captures an origin-scope lease, held only
until delivered/superseded). That dissolves R4 at the hub level and
deletes `deliver_current` outright (callers seed). See the RESOLUTION
section and `stage2_subscriber_hub_redesign.md`.

## Q2 — a supported scope/task group for mounted impls

**RESOLVED by stage 12** (`v2/plans/phase3/stage12_better_scope/`),
which landed after this section was written and is essentially option
(B) done right at the framework level — without the self-pin problem,
because the lifespan parks holding no lease (§4.6 of the design doc)
and per-call access re-authorizes from the dispatch lease instead of a
standing one. Concretely: `_fxtr_mount_lifespan(scope)` runs setup
authorized at mount time and can open an `anyio` task group that lives
for the mount's life (yielded as lifespan state, reached per-call via
`_fxtr_get_lifespan_state()`); `_fxtr_get_mount_scope()` returns the
mount scope, authorized for the call's duration. With the stage-2 hub
no longer needing an owner scope at all, the only thing a guest source
needs from "its" scope is the task group — which the lifespan
provides. (A)'s tg half is subsumed; (C)'s pin semantics were
ultimately rejected in stage 2 (subscriptions deliberately do *not*
keep the source alive); (D) and (E) are unnecessary. The ground facts
below describe the pre-stage-12 world.

### Ground facts (verified against `entity_ref_scope.py` / `service_cap_common.py`)

- A guest entrypoint mounts its impl on the **connect-dispatch call
  scope**. When the entrypoint returns, that scope's CM exits and
  `aclose` runs: `_draining=True` for as long as the peer holds the
  connected ref (the chain bridge pins the token). It is
  *drained-but-alive for essentially the impl's entire service
  life.*
- Once `_draining` is set, `check_alive()` fails: **`mount`,
  `.store()`/tracking, `copy_from_scope`, bridges, and
  `FxtrScope(include=[it])` (the unconditional constructor path,
  which clones `parent._main_lease` — already discarded) all
  raise.**
- Dispatches keep working because they take the *derived* path:
  every inbound call carries a fresh `Lease[mount.main]` (the wire
  input), and `_dispatch_body` hands it to
  `_with_derived_lifetime(derived_from_leases={mount.token: lease})`,
  which explicitly tolerates draining parents. This is the
  "rebuild the scope via its input-provided token" move.
- The mount scope's **task group stays open through the drain**
  (it closes only after the token ends), and the framework itself
  keeps `task_group.start()`-ing dispatch bodies on it during drain
  — so "spawn background tasks on the mount tg" is already
  load-bearing, just not exposed.
- The handle layer already passes the tg to the impl
  (`_mount_as_fxtr_service(*, task_group)`), so impls *can* capture
  it today by override — there's just no blessed surface, and the
  scope half of the problem is unsolved either way.
- A live derived scope's main lease **carries a via on the parent
  token** (that's the documented `subtake` shape) — i.e. a
  long-lived derived child *pins* the mount token, so the mount
  scope cannot finish draining until the child closes. Any
  "service-lifetime scope derived from the mount token" therefore
  needs an explicit release story or it deadlocks the natural
  release path (peer drops ref → token should end → can't, we pin
  it → nothing will ever signal us).
- Host-side precedent: both hub consumers
  (`basic_dynamic_dispatch`, agent MCP) have a *harness* that owns
  `impl_scope` + `impl_tg` around the impl (`async with scope, tg:`).
  The guest has no place to put that `async with` — that's
  followups item 2 verbatim.

### (A) Bless mount-time capture (baseline / strawman)

Document `_mount_as_fxtr_service`'s `task_group` (or surface it as
`self.mount_task_group` on `CommonServiceImpl`) and let impls grab
`get_active_scope()` at mount time.

- The tg half genuinely works (see ground facts).
- The scope half does **not**: by the first `subscribe` the mount
  scope is draining, so `hub.subscribe`'s
  `FxtrScope(include=[mount_scope])`, publish-time
  `copy_from_scope` into it, and handle-mounting all raise. Listed
  mainly to record *why* it's insufficient alone.

### (B) Framework-provided service-lifetime scope ("reopen the mount scope")

Opt-in on the impl/handle: at mount time (scope still alive) the
handle opens a derived child —

```python
service_scope = FxtrScope._with_derived_lifetime(
    include=[mount_scope],
    derived_from_leases={mount.token: mount._main_lease.clone()},
    mountable=True)
```

— held open by a holder task started on the mount tg, and exposed as
`self.service_scope` / `self.service_task_group`. Conceptually this
*is* "the impl reopens its mount scope via the token", done once and
owned by the framework.

- Pro: exactly the missing capability, host- and guest-uniform;
  the hub then works unmodified (`scope=service_scope,
  task_group=service_tg`); R4 is solved by copying payloads into
  `service_scope` (the agent-MCP head pattern).
- Con / the hard part: **the self-pin** (ground facts, last bullet).
  Release options:
  - *(i) cancellation-only*: holder task dies when the connection /
    mount tg is cancelled. Covers guest crash/disconnect but turns
    the *graceful* release path (peer politely drops the ref) into a
    hang — not acceptable as the only mechanism.
  - *(ii) a release signal from the service-tracking layer*: "this
    mounted service has no external holders left". The scope/lease
    layer can't express "ends when only my own children pin me"
    today; the workstation's `TrackedService.shutdown_event` is
    workstation infra, not fxtr2. Would need a real design.
  - *(iii) a weak-derived lease primitive*: a derived output lease
    that does **not** via-pin its input — input ends whenever
    external holders are gone, output then cascade-ends (forceful,
    which is fine: hub teardown is forceful by design). This makes
    the service scope's lifetime exactly "while anyone outside still
    references the service" with zero bookkeeping. Leases-layer
    feature; needs scrutiny (it weakens the complete-pin invariant
    deliberately, so what breaks?).
- Either (ii) or (iii) is a meaningful fxtr2 sub-project; (iii)
  feels like the load-bearing primitive this whole space keeps
  circling.

### (C) No impl scope; per-subscription derived scopes in the hub

Generalize the hub instead: per-subscription worker scopes are built
via the derived path from the *source impl's* mount token (a lease
for it is mintable inside the `subscribe` dispatch — the call
scope's ancestor entry supports `subtake` on the parent token; needs
a small public API). Each live subscription then pins the mount
token; unsubscribe / crash-backstop / cancel release it.

- Pro: "subscribers keep the source alive" is arguably the *correct*
  lifetime semantics, and there's no idle impl-scope sitting around;
  no new primitive — the pin is released by the already-required
  unsubscribe contract (plus the crash backstop), so no deadlock on
  the natural path.
- Con: a peer that vanishes *without* unsubscribing pins the guest
  connection until some delivery fails (crash backstop) — there may
  be no next delivery for a quiescent source; R4 is unsolved: with
  no owner scope, published/retained payload entities have no home
  once the emitting dispatch drains. Publish would have to become
  async and copy payloads into every live subscription scope (N
  copies, and re-derive seeds for late subscribers from some other
  claim-holder), which is exactly the kind of residency side-channel
  the `track_from_memory_arena` incident taught us to avoid.
- Verdict: attractive pin semantics, but incomplete without *some*
  owner scope for payloads — probably a complement to (B)/(D), not
  a standalone answer.

### (D) Adapter-level: per-connect "connection scope" for guests

Solve it where the lifecycle is actually observable: the sandbox
adapter (`fxtr_sandbox_rpc_adapter`) opens, per `connect`, a
**sibling** long-lived mountable scope + tg under `guest_host`
(*not* derived from the connect-dispatch scope — so it doesn't pin
it), and exposes it to the entrypoint (e.g.
`get_connection_context()` or an optional second entrypoint arg).
Teardown wiring: because nothing pins the connect-dispatch token,
it ends naturally when the peer releases the connected ref —
register a token **end-callback** on it that closes the sibling
scope (forcefully; hub teardown is forceful anyway). Connection
death cancels `guest_host`'s tree and gets the same result.

- Pro: no new lease primitive — composes from existing pieces
  (sibling scope + `register_end_callback`); the release signal is
  exact on both the graceful and crash paths; directly discharges
  followups item 2 ("expose the adapter's guest_host scope (or a
  child) to entrypoints"); multi-connect-per-spawn gets one scope
  per connect for free.
- Con: guest-only — host-side impls keep the harness pattern, so
  "how impls get scope+tg" stays non-uniform (maybe fine: host
  impls *have* owners); a small API design question on how the
  entrypoint receives it (ambient contextvar vs. explicit arg —
  explicit arg changes the entrypoint signature contract, ambient
  is magic); the end-callback fires from lease-machinery context
  (sync) so it can only *signal* the holder task, not await
  teardown.

### (E) First-class subscription support in the service layer

The maximal option, connecting to the stage-1 `change_events`
indirection TODO (dispatcher protos): impls declare an event-source
attribute; the dispatch/handle infrastructure owns the hub, its
scopes, and worker lifecycle internally, and protos stop
re-declaring `subscribe` by hand.

- Pro: removes both the proto-redeclaration wart and the
  impl-needs-a-scope wart *for the subscription use case*; one
  blessed implementation of the lifetime rules instead of every
  source getting them right.
- Con: doesn't give impls a general-purpose scope/tg (the next
  non-subscription background-work need hits the same wall);
  largest design+implementation lift; premature until the
  underlying lifetime story ((B)(iii) or (D)) exists for it to
  stand on.

## Candidate pairings

- **Minimal-change port:** Q1(d) snapshots + Q2(D) adapter
  connection scope. No fxtr2 core changes (adapter + demo only);
  hub used as-is in coalesce/deliver_current mode; R4 satisfied by
  copying the current run's outputs into the connection scope.
  Risk: D's API design is new surface in the adapter; nothing else
  novel.
- **Hub-generalization track:** Q1(b) retention (possibly via (c)'s
  strategy split) + Q2(B) service-lifetime scope with the
  weak-derived lease (iii). Bigger, uniform across host/guest,
  pays for itself if stream-shaped sources multiply. (iii) is the
  research-y bit and should be scoped on its own before committing.
- **Defensible middle:** Q1(a) seed param (tiny, useful regardless)
  \+ Q2(D). Keeps fine-grained events if we decide the stream shape
  matters; the scheduler keeps a replay buffer in its connection
  scope.

In all pairings the wire/proto surface is the same (re-declared
`subscribe` returning a handle; frontend `subscribe_to` flow;
same-script `create_callback` + `subscribe_to` agent-side), so the
demo-facing docs/renderer work is identical and can't be
invalidated by the choice.

## Open questions — all resolved 2026-06-10

1. ~~Q1: second stream-shaped source / snapshot-shaped contract?~~ —
   The demo keeps fine-grained delta events in queue mode (first real
   queue consumer), with a `run_state` snapshot as the *seed* only:
   the (a)+(d) hybrid from the leaning.
2. ~~Q2(B)(iii) weak-derived lease~~ — not needed for any of this
   (stage 12 + the stage-2 topology sidestep it); still the candidate
   primitive for a future general "ends when only internal machinery
   pins it" lifetime, recorded in stage 2's design notes by way of
   the holding-scope pattern being a working miniature of it.
3. ~~Q2(D) entrypoint API shape~~ — moot; (D) is not built. The
   lifespan hook is the guest story.
4. ~~Host-impl story~~ — uniform with guests via the same lifespan
   hook; existing host harnesses may stay on the harness pattern
   indefinitely (stage 2 migrates them only off the removed hub
   args, not onto lifespans).
5. ~~Retention/queue bounds~~ — the replay buffer is gone (snapshot
   seed instead); per-event origin leases self-bound in coalesce mode
   and are documented as pinning origins in queue mode; scheduler
   retention is exactly one run (retained lease, swapped at run
   start). The general queue-bounds TODO remains on the hub, now
   smaller in scope.
6. Port-specific decisions stand as listed (hub-backed
   `debug_state`, `SimpleEventListenerProto` deleted, renderer
   storage version bump, haiku/poem + in-process test dropped) —
   carried into `stage3_demo_refactor.md`.
