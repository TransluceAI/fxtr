# Stage 3 — Demo refactor: porting the scheduler prototype onto the new subscriber system

Port the `~/tmp/fxtr-demo-workdir-workflowtest` scheduler prototype
(simple workflow scheduler + task system; see
`SCHEDULER_PROTOTYPE_STATUS.md` in that workdir) into
`examples/fxtr_workstation_demo/agent_workdir/`, re-authored on:

- the workstation part 6 stage-1 subscriber system (`Subscriber` / `UnsubscribeHandle` /
  `EventSource`, inbox `subscribe_to`, `CallbackInvokeOp`),
- the workstation part 6 **stage-2 hub redesign** (seeded delivery, origin-lease
  residency, non-pinning subscriptions, client-level close messages)
  — *prerequisite: stage 2 must land first*,
- the fxtr2 phase 3 stage-12 scope redesign (`_fxtr_mount_lifespan`, per-call
  mount-scope re-auth, `clone_authorization`) — this is the **first
  real-world consumer of the mount-lifespan hook**.

Scoping/rationale record: `stage1post1_demo_refactor_scoping.md`.
The workdir prototype stays as-is as a historical reference (not
re-pinned, not kept runnable); the port is authored fresh in the
examples tree.

Already-decided drops (2026-06-06/10, revised in review): `haiku_task`,
the Poem entity + renderer, and the in-process scheduler test are
**not ported**. `prompt_task` + the `PromptTemplate` entity **are**
ported (the generalized LLM step; the examples tree's existing
`haiku_service_wasm` / `haiku_service_oci` serve as its ask-service, so
the prototype's OCI haiku service needs no port of its own).
`SimpleEventListenerProto` is deleted (its pattern died
in stage 1). The parked-subscribe workaround and the replay buffer are
deleted (subsumed by hub mechanics below).

## What the ported demo is

Same demo shape, minus the poem task: a wasm `simple_scheduler` service
runs named inputs through a sequential chain of wasm `SimpleTaskProto`
tasks (rows concurrent, one run at a time), streams progress to a
frontend grid panel via the workstation inbox, and returns a durable
`SimpleRunResult`. Core pipeline: ~10 Notes → `note_transform` →
shouted notes in the grid. `prompt_task` (Note → Note through an LLM
ask-service with a stored `PromptTemplate`) rides along as the
optional LLM step, connecting to the tree's existing haiku services.

## Design

### Event contract: snapshot-first, then deltas (queue mode)

- The scheduler's hub runs `mode="queue"` (first real queue-mode
  consumer). Fine-grained events keep their prototype shapes
  (`run_started`, `task_started`, `task_result`, `task_failed`,
  `run_completed`), documented in the marker package.
- New event kind `run_state`: a full run-state snapshot. Shape mirrors
  the renderer's fold result so "snapshot reinit" is a trivial state
  replacement: the `run_started` header fields (`run_id`,
  `step_labels`, `steps`, `input_names`, `inputs`) plus
  `cells: {input_name: {str(step_index): {"status": "waiting" |
  "running" | "done" | "failed", "output"?: FxtrID, "error"?: str}}}`
  and `completed: {"n_ok", "n_failed", "result"} | None`. Delivered
  **once, as the seed**, to every new subscriber; never published on
  the live stream. Built synchronously from the impl's bookkeeping at
  subscribe time (sync ⇒ atomic vs. concurrent publishes ⇒ gapless
  handoff per the stage-2 recommendation).
- New event kind `scheduler_closing`: the client-level close message
  (stage-2 D5), published with `origin_scope=None` from the mount
  lifespan teardown, followed by `hub.aclose()`.
- A panel opened after a finished run gets the completed grid via the
  snapshot (strictly better than the prototype's "no run yet").

### Scheduler impl: lifespan-owned state, per-run retention scope

*(Revised mid-implementation — see "retained-lease deadlock" under
Risks. The originally-planned zero-copy retained-lease retention is
**dead**: a live `Lease[call_scope.main]` carries `Lease[mount.main]`
as a via (`service_cap_common._dispatch_body`), so retaining a
run-scope lease pins the mount token, and graceful shutdown — which is
gated on mount drain — can never reach the teardown that would drop
the lease. Confirmed empirically: the impl tests deadlocked at
source-teardown after any completed run. The plan's named fallback
(copies, released at the next run start) is adopted, upgraded from
"copy into the mount scope" to a per-run isolated retention scope.)*

`_SchedulerImpl(TypedServiceImpl[...])`, nothing stashed on `self`:

```python
@contextlib.asynccontextmanager
async def _fxtr_mount_lifespan(self, scope):
    async with anyio.create_task_group() as tg:
        state = _SchedulerState(hub=SubscriberHub(task_group=tg, mode="queue"), tg=tg)
        try:
            yield state
        finally:
            try:
                state.hub.publish({"kind": "scheduler_closing"}, origin_scope=None)
                await state.hub.aclose()
            finally:
                state.release_current_retention()
```

- `_SchedulerState` (the lifespan state, reached in methods via
  `self._fxtr_get_lifespan_state()`): the hub, the lifespan task
  group, the run lock, the folded current-run bookkeeping (what
  `run_state` snapshots are built from, including its retention
  scope), and the small debug-error ring.
- **Retention = one run, via a per-run retention scope**: at run
  start (under the run lock) a holder task on the lifespan tg opens an
  isolated scope (`include=[GLOBAL_SCOPE]` — pins nothing of the
  mount's, mirroring the hub's sub-scope topology) and parks until its
  release event fires; the previous run's event is fired once the new
  bookkeeping is swapped in (its scope then drains gracefully — any
  still-pending deliveries hold event leases on it — and frees that
  run's copies). The step `ServiceRef`s are bridged into the scope at
  run start (`independent_lease` on the task services' own tokens) and
  the inputs copied in; each step output is copied in as it lands,
  before its `task_result` is published.
- **Every publish and seed uses the retention scope as origin**
  (`origin_scope=run.retention_scope`), so payload entities stay
  reachable for slow/queued deliveries and post-run seeds without the
  run dispatch's call scope ever being pinned — one
  `copy_from_scope` per input/output is the price (demo-scale
  trivial; this is the prototype's mid-run replay mechanism, now
  applied uniformly). `_emit` stays sync (fold + publish; the copies
  happen in the chain runner *before* the emit).
- `subscribe` (re-declared on the proto, see below) needs no
  re-authorization dance — the retention scope's base is live until
  the swap/teardown releases it:

  ```python
  state = self._fxtr_get_lifespan_state()
  run = state.run
  if run is None:
      return await state.hub.subscribe(subscriber, get_active_scope())
  return await state.hub.subscribe(subscriber, get_active_scope(),
                                   seed=[run.build_snapshot()],
                                   seed_origin=run.retention_scope)
  ```
- `debug_state` shrinks to hub-backed counts
  (`live_subscription_count`, last errors); the listener/replay-buffer
  fields are gone.
- Lifecycle properties inherited from stage 2, for the README: the
  frontend's subscription does not keep the scheduler alive; dropping
  the scheduler ref (e.g. the agent forgets it / session closes)
  drains the mount, subscribers get `scheduler_closing`, everything
  unwinds.

### Why retention exists (what the retention scope enables)

*(Added in review, 2026-06-10 — the mental model for why any retention
machinery is needed at all.)*

Retention exists **solely so a subscription created after (or
between) runs can be seeded** with a `run_state` snapshot whose
entity refs still resolve. Every other flow rides the run dispatch's
call scope plus the hub's event-borne residency, with no impl-side
retention:

- Mid-run subscribers seed from the live run scope (base live).
- In-flight deliveries to slow subscribers hold per-event origin
  leases, released on delivery (hub-owned, bounded).
- Already-delivered events are durable with no help — the
  workstation's delivery deep-copies payloads into the session event
  log, so an already-subscribed panel keeps its grid forever.
- The returned `SimpleRunResult` is the dispatch's return value,
  copied out on invoke-result delivery — `retrieve_result` works
  after the scheduler is gone.

The one thing that breaks without retention: a panel opened (or
closed + reopened — each reopen is a fresh inbox + subscription)
*after* `run_batched` returned. Its seed references output/input
`FxtrID`s, and once the run scope drains there is no origin to
deliver them from. Serving the completed grid to late panels is R2
from the scoping doc plus a deliberate upgrade over the prototype's
"no run yet".

This also makes retention **cleanly detachable**: dropping the
feature would mean `_emit` publishes with the default origin (the
run dispatch's call scope), the post-run seed branch returns
nothing, and the holder/copies/bridging machinery disappears — the
bookkeeping fold, hub usage, event contract, and renderer are
untouched. Conversely, that detachability is why copying outputs
*into* a scheduler-owned scope (rather than pinning the producers'
scopes for zero-copy) is the right default: each step's resources
release the moment its dispatch drains, regardless of who is still
reading the scheduler's retained snapshot.

### Protocols / marker package (`simple_tasks`)

- `SimpleTaskProto` unchanged. `SimpleRunResult` / `WrappedEntity` /
  `NoteWithLinks` / `PromptTemplate` entities unchanged (minus Poem).
- `SimpleTaskSequenceSchedulerProto`: `run_batched` unchanged;
  `subscribe(listener) -> None` (parked) is replaced by the
  re-declared

  ```python
  async def subscribe(self, subscriber: ServiceRef[Subscriber[Any]]) -> ServiceRef[UnsubscribeHandle]: ...
  ```

  (re-declared, not subclassed from `EventSource` — registry rejects
  subtype registration; method must be named `subscribe`, same as the
  dispatcher protos). `SimpleEventListenerProto` deleted.
- Event-shape docs move with the protocols; document `run_state` /
  `scheduler_closing` alongside the delta kinds.

### Frontend / renderers

- Subscribe flow is the standard stage-1 chunk-5 pattern (same as the
  dynamic dispatcher / agent panel): same-tick `allocateCallback()` +
  `allocateInvoke(inbox.handle, "subscribe_to", [schedulerRef])`
  against the exported scheduler ref; teardown via `CloseCallbackOp`.
  No frontend-machinery changes expected.
- Grid renderer: derive state as *snapshot reinit + delta fold* —
  first `run_state` event (or any later one, defensively) replaces
  state; delta events fold as today; `scheduler_closing` renders a
  "scheduler shut down" badge and stops expecting events. Storage
  version bumps. Poem renderer dropped.

### Packaging / wiring (mechanical)

Mirrors the prototype's layout, adapted to the examples tree's
conventions (template-form `# fxtr-lib-sub:` lines, no committed
`uv.lock`s — see `agent_workdir/packages/README.md` for the three venv
contexts and shapes):

- `packages/simple_tasks/` (marker, host-importable, joins the
  `type_entities/` aggregator), `packages/simple_task_lib/` (wasm
  tasks: `transcript_to_note`, `wrap_entity`, `note_transform`;
  single-pyproject shape, like the prototype),
  `packages/prompt_task/` (wasm, single-pyproject shape),
  `packages/simple_scheduler/` — **split shape** (decided in review):
  marker-style root holding the impl code (host-importable, so the
  chunk-2 impl tests can import it) + `sandbox_spawn/` wasm env
  declaring deps and the `fxtr.service` entry point, like
  `delayed_echo/`.
- `renderers/simple_scheduler/` + `renderers/simple_run_result/`
  (ported too — the grid's "full result entity" link and
  `retrieve_result` render through it) + preset slices; flake wiring
  for the wasm spawns and the new typeEntities.
- The lib pin must include stage 2 (and stage 12); guests get the new
  hub via fxtr2-portable's `fxtr2/services` force-include
  (verified in stage 1 chunk 4).

### Host-side tests (decided in review)

The examples tree had no test conventions; this stage establishes
them: `examples/fxtr_workstation_demo/tests/` riding the demo's
existing host pyproject (full fxtr2 + workstation deps; add a pytest
dev group). A small `conftest.py` makes the agent packages importable
(`sys.path` inserts to `agent_workdir/packages/*/src`, same spirit as
`run_demo_workstation.py`). This is where chunk 1's entity round-trip
test and chunk 2's scheduler impl tests live.

### `scaffold_workdir.py` repair (decided in review)

`prepare_workdir_repo` became async (the flake lock runs through the
`fxtr_nix_runner` backend) and `scaffold_workdir.py` still calls it
synchronously — currently broken. Fix the call (`asyncio.run` /
`anyio.run`) and the stale "stdlib-only" comment (the harness now
imports `fxtr_nix_runner`). The jj-workspace lib-ref resolution
(working-copy `@` pushed to the harness's bare mirror) already lives
in `resolve_lib_ref`, so scaffolding straight from the jj primary
checkout works once the call is fixed.

## Implementation chunks

### Chunk 1 — marker + task packages

- [x] `packages/simple_tasks/`: protocols (re-declared `subscribe`),
      entities (incl. `PromptTemplate`, minus `Poem`), pinned type
      ids, event-shape docs (incl. `run_state`, `scheduler_closing`);
      delete `SimpleEventListenerProto` and the replay-semantics docs.
      Join the `type_entities/` aggregator. (Type-id strings verified
      identical to the prototype's flake pins.)
- [x] `packages/simple_task_lib/` ported (minus nothing — it had no
      subscription coupling); `packages/prompt_task/` ported.
- [x] Test scaffolding (`tests/` + pytest dev group + conftest
      `sys.path` inserts) + the entity round-trip test.

### Chunk 2 — scheduler impl

- [x] `_SchedulerImpl` per the (revised) design (split-shape package:
      impl in the marker root, `sandbox_spawn/` wasm env):
      lifespan-owned `_SchedulerState`, queue-mode hub, sync `_emit`,
      per-run retention scope (replacing the deadlock-prone
      retained-lease design — see Risks), snapshot-seeded
      `subscribe`, close message in teardown, hub-backed
      `debug_state`.
- [x] Targeted impl tests in `tests/` (snapshot fold correctness,
      retention swap, seed-on-subscribe incl. `ServiceRef`s-in-seed,
      mid-run seed/delta composition vs. a from-start subscriber,
      failure cells, unsubscribe, and the full source-teardown
      `scheduler_closing` walkthrough — also implicitly re-exercised
      by every harness teardown); live behavior is otherwise covered
      by chunk 4. The scheduler is mounted guest-style in the harness
      (mount scope draining from the moment the ref is handed out).

### Chunk 3 — renderers + flake

- [x] Grid renderer: snapshot reinit + delta fold + closing badge;
      storage version bump (`simple_scheduler/v3`); subscribe via
      same-tick `allocateCallback()` +
      `allocateInvoke(inbox.handle, "subscribe_to", [schedulerRef])`.
      `simple_run_result` renderer ported as-is; `note_with_links`
      renderer (NoteWithLinks + PromptTemplate) ported too — the grid
      cells and prompt_task outputs render through it.
- [x] Flake wiring for `simple_task_lib` + `prompt_task` +
      `simple_scheduler` spawns (scheduler `src` points at
      `sandbox_spawn/`) + the four new typeEntities + preset slices;
      template-form lib refs throughout (id strings copied from the
      prototype's flake, re-verified against the ported package).

### Chunk 4 — workdir prep + docs (live validation deferred)

- [x] Fix `scaffold_workdir.py` (await the now-async
      `prepare_workdir_repo` via `asyncio.run`; stale "stdlib-only"
      comment — the harness imports `fxtr_nix_runner`, resolved from
      the demo venv). The jj-workspace lib-ref resolution needed no
      work — `resolve_lib_ref` already pushes `@` to the bare mirror.
      Scaffolded a runnable workdir at
      `~/tmp/fxtr-demo-workdir-stage3` (lib pinned to the ws 6.3
      tree via the mirror; all locks generated) for the live
      validation session.
- [x] `agent_workdir/simple_workflow_guide.md`: the from-scratch agent
      guide (run the canned demo end-to-end, agent-side subscription,
      lifecycle notes, prompt_task usage, define-your-own-task loop
      incl. entity types / renderer / flake wiring) — written against
      the *ported* code, leaning on the README for generic mechanics.
- [x] README updates: `agent_workdir/README.md` (flake bullet, new
      package + renderer entries, guide pointer),
      `agent_workdir/packages/README.md` (shape examples), demo
      `README.md` (layout tree incl. `tests/`).
- [x] `projects/fxtr_workstation/CODE_OVERVIEW.md` checked — it does
      not reference the examples demo's capabilities (only in-package
      demos), so no change needed; the demo README is the demo's
      overview doc.
- [ ] **Deferred to a separate session (Daniel):** live run-through
      against a workstation (adapted from the status doc's "exact
      remaining steps", minus the poem task): spawn the wasm services
      at `current_commit()`, connect (`note_transform`; scheduler
      `connect(None)`; optionally `prompt_task` against a haiku
      service), `export_service` + `present_to_user`, open the panel
      (snapshot: empty → grid), fire `run_batched` via
      `schedule_invoke`, watch deltas fill the grid,
      `retrieve_result` → load `SimpleRunResult`. Plus the new
      lifecycle: open a second panel mid-run (snapshot seed), close a
      panel without unsubscribing (source-side teardown via handle
      drop), forget the scheduler ref (subscribers receive
      `scheduler_closing`).

## Risks / things to verify

- ~~**Retained-lease retention** rests on stage 2's drained-origin
  delivery verify item (a drained call scope's arena stays loadable
  via a retained lease). If it fails, fallback: copy outputs into the
  mount scope at publish + abandon at next run start (rejected
  design, kept as the named fallback).~~ **Failed for a different
  reason — the retained-lease deadlock** (found in chunk 2): the
  retained `Lease[call_scope.main]` carries `Lease[mount.main]` as a
  via, pinning the mount token, while the only code that drops it (the
  lifespan teardown) is gated on the mount token draining. The impl
  tests reproduced the hang (source-teardown after any completed run
  never finished; the no-run path was fine). The fallback was adopted,
  upgraded to a lifespan-owned **per-run retention scope** — see the
  revised design section.
- **Snapshot consistency**: snapshot build and the `_emit` fold are
  both sync on one event loop — assert (test) that a mid-run
  subscriber's seed + subsequent deltas compose to the same grid as a
  from-start subscriber.
- **Run-lock + retention interplay**: releasing the previous run's
  retention scope at next run start happens under the run lock; a
  subscriber arriving between runs must seed from the *previous*
  run's snapshot (retained) — make sure the swap order preserves that
  (the release fires only after the new bookkeeping is swapped in,
  and the released scope drains gracefully under any still-pending
  seed deliveries).
- ~~**`ServiceRef`s in the post-run seed**: the `run_state` snapshot
  carries the `steps` list (`ServiceRef`s that arrived as
  `run_batched` dispatch args, living in the retained drained run
  scope) — stage 2's drained-origin tests covered entity payloads,
  not service-ref re-encode through a retained lease.~~ Obsoleted by
  the retention-scope revision: the steps refs are bridged into the
  (live, non-draining) per-run retention scope at run start, so seed
  delivery encodes them through an ordinary live origin. Covered by
  the post-run-seed impl test.
- **Guest-side first**: this is the first `_fxtr_mount_lifespan`
  consumer *and* the first guest-side hub. The stage-2 test suite is
  host-side; chunk 4's live validation is where guest-specific
  surprises (wasm, chain latencies) will surface — budget for it.
- MCP relay gotchas from the prototype still apply (exported-service
  dispatches relay through the MCP chain; don't batch invokes against
  a freshly-connected service in one mutation).

## Implementation notes (running log)

- **The retained-lease deadlock** (the headline finding; details in
  the design + risks sections): confirmed empirically before the
  redesign — the no-run source-teardown test passed in 0.11s while
  any test with a completed run hung forever at teardown. Worth
  remembering as a general rule: *retaining any
  `Lease[call_scope.main]` beyond the dispatch pins the mount token*
  (it vias `Lease[mount.main]`), so per-call leases are only safe to
  hold transiently (the hub's pending-event origin leases are fine;
  a standing retention is not).
- **Duplicate step refs need bridge dedup**: the same task ref at
  several `steps` positions hits "Service … is already registered"
  on the second `bridge_service_from_scope` into the retention
  scope — deduped by `ServiceKey`. (`copy_from_scope` is fine with
  duplicate entity ids.)
- **Impl-test harness shape**: the scheduler is mounted guest-style
  (host task exits its mount-scope CM immediately after bridging the
  ref out, so the mount is draining for the impl's whole life — the
  connect-dispatch reality), with the ref keepalive in a dedicated
  holder scope and a `work` scope (`include=[ref_scope, caller]`)
  for dispatches. Every harness teardown walks the full
  source-shutdown path, which makes the suite a standing regression
  test for the drain topology.
- **Live-stream origins are now also the retention scope** (not the
  run dispatch's call scope as in the original design): outputs are
  copied in before their `task_result` publishes, so queue-mode
  pending deliveries never pin the mount even transiently.
- `examples/` is outside every pre-commit pyright hook, so the
  sys.path-style imports show as unresolved in the IDE only (same as
  the pre-existing `run_demo_workstation.py` pattern).
- The scaffolded validation workdir pins the lib to the jj
  working-copy commit at scaffold time (an empty change whose tree ==
  the ws 6.3 commit) via the harness mirror
  (`~/.cache/fxtr-workdir-harness/lib-mirror.git`).

## Follow-up: scheduler retention onto the per-call input scope — ATTEMPTED, then REVERTED (2026-06-10)

The retained-lease deadlock motivated a framework feature —
`v2/plans/phase3/stage12_better_scope/input_scope_plan.md` splits each
dispatch into a lifespanless **input scope** (owns the call's wire
inputs; non-mount-pinning; retainable) plus the call scope deriving
from `{mount, input}`. The plan's decision point read: "skip if the
caller-pinning trade reads as a regression" — and the attempt showed
it is one, structurally, so the scheduler **stays on the copies-based
per-run retention scope**.

What the attempt found (the migration itself was mechanical and
worked: retain `input_scope.clone_authorization()` per run, args
native, outputs copied in, `origin=input_scope` everywhere): run
retention pins the run's *sender* (the caller-side scope that
exported the `run_batched` args) until the next run or the
scheduler's teardown. That composes into a **teardown deadlock
cycle** with a pre-existing semantics: a caller holding the
*returned* `SimpleRunResult` arena (any caller — just awaiting
`run_batched` installs it) holds an at-rest `Lease[mount.main]`
clone in its chain bridge, released only at caller **close**; the
caller's close is gated on its **drain**; its drain is gated on the
retention; the retention's release is gated on the scheduler's mount
draining — which waits for that very `Lease[mount.main]`. So any
in-process caller that ran a batch and tried to unwind deadlocked
(the migrated impl tests reproduced it immediately). The copies-based
retention pins nobody, so no cycle exists.

Carried back into `input_scope_plan.md` as a documented caveat on the
retention pattern: input-scope retention by a service whose callers
hold its dependent returns across teardown creates this cycle —
prefer copies into an impl-owned scope in that shape.

Rejected-for-here alternative, recorded in `input_scope_plan.md`
("fully zero-copy retention"): a per-run TG-bearing scope parented on
the input scope only, with step proxies resolved into it so outputs
land there zero-copy. Works structurally, but it pins the *step
services'* reply scopes for the retention window (outputs stay
chain-loads against the steps; the steps can't dispose) — copying
outputs out deliberately decouples those lifetimes, letting each step
GC its results the moment its dispatch drains.
