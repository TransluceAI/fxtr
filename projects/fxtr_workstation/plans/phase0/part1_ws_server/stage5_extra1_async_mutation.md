# Stage 5 — extra 1: async mutation pipeline + per-session queue

Refactor `WorkstationState.apply_mutation` from sync to async, route
mutation requests through a per-session queue, and reshape the
callback / invoke arg handling around the new model. The aim is to
collapse a chunk of accidental complexity that landed in 5b/5c
(deferred-binding `CallbackRecord` placeholders, the two-walk codec
dance in invoke args, the within-mutation race between
`_walk_callback_records` substitution and the slot-claim
machinery).

This doc is a stage-5 *interlude*: it lands between 5c and 5d and
reshapes the foundation that 5d (`notify` 3-phase delivery) will
ride on.

## Motivation

Two concrete pain points in the 5b/5c shape:

1. **Async resolution leaks into the codec.** `unescape` returns
   a heterogenous tree where `["callback", slot]` becomes a
   `CallbackRecord` placeholder rather than a `ServiceRef`. The
   invoke worker has to walk the args a *second* time to await
   each placeholder's `ready` event and substitute in a real
   `ServiceRef`. The `pending` / `ready` / `cancelled` lifecycle
   on `CallbackRecord` is purely scaffolding for that
   asynchronous resolution — it isn't load-bearing for any
   user-visible semantics.

2. **Within-mutation arg races.** The current sync
   `apply_mutation` collects `invoke_specs` during op-validation
   and spawns workers at commit time. The worker then awaits
   bridges and looks up arg services in `state.service_records`.
   Between op-validation and that lookup, anything can happen —
   including an `UntrackServiceOp` in *the same mutation* that
   releases the service the invoke was about to use. Today this
   surfaces as `_deliver_invoke_error("service no longer
   tracked")`, which is correct but obscure. Cross-mutation it's
   the same shape; cross-session it's also the same shape.

A three-phase async pipeline + per-session queue addresses both:
the codec returns real `ServiceRef`s because callbacks are fully
ready by the time their slot is referenced; and arg refs are
incref'd against the calling session synchronously inside the
commit, so a later `untrack` in the same mutation just
decrements rather than releasing.

## Three-phase `apply_mutation`

```
async def apply_mutation(self, request):
    # 1. Validate (sync)
    # 2. Async pre-commit (rollbackable)
    # 3. Commit (sync, always commits)
```

### Phase 1 — Validate (sync, no state changes)

Mirrors today's op-validation pass with two shape changes:

- Walks `InvokeOp.args` for *structural* checks only — every
  `["entity"|"service", short_id]` resolves in the workstation
  registries; every `["callback", slot]` resolves either against
  `record.open_callbacks` or against a working set of slots
  about to be created earlier in this mutation. Decoded values
  are not stored — phase 3 redecodes with real lookups, which
  by then have fully-built `CallbackRecord`s available. (Net:
  one extra walk over args, but the walks are single-purpose
  and homogenous in their output.)
- Stale-ack check, slot monotonicity, double-track validation,
  etc. all unchanged in shape.

Failure here returns a `MutationRejectedResponse` with a
specific reason (see below); no state has been touched.

### Phase 2 — Async pre-commit (rollbackable)

Today's only async-while-rollbackable work is
`CreateCallbackOp` bridge setup. For each `CreateCallbackOp` in
the mutation, in order:

- Open `impl_scope`, mount a `WorkstationCallbackImpl`, run
  `get_or_create_service_record` (which spawns the standard
  `service_record_worker`), and `await tracked.bridge_ready`.
- On success: collect the just-built (`slot`, `CallbackRecord`)
  pair into a "creates to attach in commit" list. The
  `CallbackRecord` is fully populated at this point —
  `tracked` / `service_key` / `service_short_id` all set, no
  `state` / `ready` field — and **not yet attached to
  `session.open_callbacks`**. We attach in the commit phase so
  an outside observer never sees a half-applied mutation.
- On failure: drain everything we built so far in this mutation
  (release each previously-collected `tracked` via
  `maybe_release_service_record` and let their workers drain
  the scopes), then return
  `MutationRejectedResponse(reason="callback_setup_failed", message=...)`.
  No further state changes; phase 3 doesn't run.

Multiple `CreateCallbackOp`s could be awaited in parallel in
principle, but sequential is simpler and keeps rollback
straightforward. Defer parallelism to a follow-up if it ever
matters.

### Phase 3 — Commit (sync, always commits)

By construction this phase succeeds. Anything that could fail
already ran in phase 1 or 2.

Order of steps:

```
a. Apply JSON patch
b. Apply tracker adds (track_entity / track_service)
c. Attach pre-built callbacks to session.open_callbacks
d. For each invoke / load spec: incref calling session against
   the spec's arg refs (entities + services). The incref is the
   "in-flight delivery contribution" for this invoke / load —
   it lives on session.service_refcounts /
   session.entity_refcounts and is dropped by the worker when
   the result event commits (or errors out).
e. Apply tracker removes (untrack_entity / untrack_service /
   close_callback)
f. Spawn invoke / load workers via state.background_tg.start_soon
g. Build + append + broadcast StateUpdateEvent
h. Send MutationCommittedResponse to mutator
```

Step (d)-before-(e) is the within-mutation race fix: a session
that does `[InvokeOp uses S, UntrackServiceOp on S]` ends up
with the invoke holding an in-flight delivery contribution on
`S` before the untrack decrements the explicit pin contribution.
The service stays alive across the untrack.

Step (c)'s "attach pre-built callbacks" is just the
`session.open_callbacks[slot] = callback_record` assignment —
all the real work happened in phase 2.

The whole phase is sync, so `start_soon`'d workers can't run
until the session worker yields back to the event loop after
commit returns. Wire ordering between `StateUpdateEvent` and any
worker-emitted events is preserved trivially.

## Per-session queue + worker

```
WorkstationState
├── sessions: dict[UUID, SessionRecord]
└── session_mutation_queues: dict[UUID, MemoryObjectSendStream[...]]
                                         ^^^ paired with a receive
                                             stream owned by the
                                             session worker
```

- One bounded `MemoryObjectStream` per session, capacity 64.
  Created in `create_session`, closed when the session is
  torn down (CloseOp commit, or server shutdown).
- One long-lived task per session running `session_worker(state,
  session_id)`, spawned on `state.background_tg`. Body is just
  "loop: receive a request from the queue, await
  `apply_mutation(request)`, repeat."
- `_handle_mutate_session` (in `client_app.py`) becomes a
  one-liner that does `queue.send_nowait(request)`. On
  `WouldBlock` (queue full): immediately reply with
  `MutationRejectedResponse(reason="session_busy", ...)`. On
  the queue being closed (session gone): reply with
  `MutationRejectedResponse(reason="session_gone", ...)`.
- Stale-ack rejections are still produced inside `apply_mutation`
  (phase 1) — they go via the standard `MutationRejectedResponse`
  path, just from the session worker's task instead of the
  client connection's read loop.

Mutations on different sessions run on different worker tasks
and proceed concurrently. Mutations within a session are
serialized through their queue.

## `MutationRejectedResponse`

New tagged-union response variant on `/client`. Carries a
`reason` discriminator and an optional `message`:

```
class MutationRejectedResponse(msgspec.Struct, ...):
    request_id: int
    reason: Literal[
        "stale_ack",
        "conflict",
        "validation_failed",
        "callback_setup_failed",
        "session_busy",
        "session_gone",
    ]
    message: str = ""
```

Reason codes:

- `stale_ack` — `ack_seq < session.last_state_ack_seq` (below the
  retention horizon). Today this is a generic `ErrorResponse`.
- `conflict` — `ack_seq < session.last_state_seq` and the last
  mutator was someone else.
- `validation_failed` — op-validation failed (unknown short_id,
  non-monotonic slot, malformed args, etc.). `message` carries
  the specific failure.
- `callback_setup_failed` — phase 2 bridge setup failed.
  `message` carries the underlying error. Should be very rare —
  this is the workstation's own internal bridge.
- `session_busy` — queue full (backpressure).
- `session_gone` — session closed before this mutation got to
  apply.

Existing `ErrorResponse` continues to handle transport-level
failures: malformed JSON, unknown ops, missing required fields.
The mutation-pipeline failures all migrate to
`MutationRejectedResponse`. Clients update their dispatch table
once.

## Removals from 5b / 5c

The 5b/5c shape survives in spirit but a chunk of scaffolding
goes away:

- `CallbackRecord.state` (`pending` / `ready` / `cancelled`)
  field — gone.
- `CallbackRecord.ready` event — gone.
- `_finalize_callback_ready`, `_finalize_callback_failure` —
  gone. Replaced by inline async code in phase 2's
  `apply_mutation`.
- `callback_record_worker`'s "wait for ready then finalize"
  shape — replaced by a smaller worker that just owns
  `impl_scope` for the lifetime of the callback (opens scope,
  mounts impl, awaits bridge, populates the `CallbackRecord`,
  signals via `task_status.started()`, parks on
  `shutdown_event`, drains). See "Worker shape changes" below.
- `_walk_callback_records` / `_substitute_callback_records` —
  gone. The codec's `lookup_callback` returns a `ServiceRef`
  directly because the callback is fully built by the time
  phase 3's args walk runs.
- The invoke worker's callback pre-phase (the substitution
  walk) — gone.
- `wire_escape.unescape`'s `lookup_callback` parameter type
  changes from `Callable[[int], Any]` (returning a placeholder)
  to `Callable[[int], ServiceRef[Any]]` (returning the real
  ref).

The session-close sweep over `open_callbacks` (in
`_close_session_drain_trackers`) stays but simplifies: every
`CallbackRecord` we encounter is fully built (no `pending`
state to handle), so the sweep is a straightforward "set
`impl._closed = True`, decref via the existing service drain."

## Worker shape changes

### `invoke_worker`

The current shape:

```
async def invoke_worker(state, session_id, slot, service_key,
                        method_name, resolved_args):
    target_record = state.service_records.get(service_key)  # race!
    if target_record is None: error
    target_scope = await _await_service_bridge_or_raise(target_record)
    callback_pre_phase(...)  # walks args, awaits, substitutes
    walk args for ServiceRefs and await each bridge
    open call_scope
    dispatch
    deliver_invoke_result_to_slot(...)
```

The redesigned shape:

```
async def invoke_worker(state, session_id, slot, service_key,
                        method_name, resolved_args, in_flight_arg_refs):
    try:
        # service_key is guaranteed to be in state.service_records:
        # apply_mutation incref'd the calling session against it before
        # spawning us, so the in-flight arg ref keeps it alive.
        target_record = state.service_records[service_key]
        target_scope = await _await_service_bridge_or_raise(target_record)
        walk args for ServiceRefs and await each bridge
        open call_scope
        dispatch
        deliver_invoke_result_to_slot(...)
    finally:
        # Always drop the in-flight arg ref contributions, even if
        # the worker errored or was cancelled mid-flight. Otherwise
        # an unexpected exception would leak refcounts and pin
        # services that nobody else is keeping alive.
        decref in_flight_arg_refs
```

Notes:

- `resolved_args` no longer contains `CallbackRecord`
  placeholders — phase 3's args walk produced real `ServiceRef`s
  via the now-trivial `lookup_callback`.
- `in_flight_arg_refs` is a `(set[FxtrID], set[ServiceKey])`
  pair carrying everything the calling session was incref'd
  against during phase 3 (d). The worker decref's these in a
  `finally` block so cancellation / dispatch errors don't leak
  the contributions. (Today there's no analog because args
  aren't pinned by the calling session at all.)

### `load_worker`

Same shape change: the entity short_id arg is incref'd by
phase 3 (d), so the worker doesn't need to claim it. A
`session_id`-keyed in-flight contribution gets dropped in a
`finally` block, mirroring `invoke_worker`'s pattern.

### Callback worker

We keep a tiny per-callback worker whose only job is to own
`impl_scope` for the lifetime of the callback. Without it,
nothing holds `impl_scope` open across awaits — `apply_mutation`
itself can't, since the scope needs to outlive the mutation.

The setup-done handshake uses `task_group.start()`'s built-in
`task_status.started()` rather than a hand-rolled event:

```
async def callback_record_worker(state, callback_record, *,
                                  task_status):
    async with FxtrScope(...) as impl_scope:
        impl = WorkstationCallbackImpl(state, ...)
        impl_ref = impl_scope.mount(impl)
        tracked = get_or_create_service_record(state, impl_ref, impl_scope)
        await tracked.bridge_ready.wait()
        if tracked.bridge_error is not None:
            raise tracked.bridge_error
        # Populate the slot-side handle. After this point the
        # callback is fully formed and reusable in invoke args.
        callback_record.impl = impl
        callback_record.tracked = tracked
        callback_record.service_key = tracked.service_key
        callback_record.service_short_id = tracked.short_id
        task_status.started()
        # Park until the open-callback contribution drops to 0.
        await tracked.shutdown_event.wait()
```

`apply_mutation`'s phase 2 calls
`await state.background_tg.start(callback_record_worker, state,
callback_record)`. `start()` raises any exception the worker
hits before `task_status.started()`, so a bridge failure
naturally propagates back into `apply_mutation` for rollback.
Once `start()` returns, the worker is parked on
`shutdown_event` and the callback record is fully populated.

No `_finalize_*` dance, no `pending`/`ready`/`cancelled`
machine — the slot-side handle is either fully built (worker
parked) or never existed (worker raised before `started()`).

## Migration order

The redesign touches a lot of files but most changes are
local. Suggested commit-by-commit order:

1. **Add `MutationRejectedResponse`.** Plus the reason-code
   `Literal`. Update the existing stale-ack / conflict /
   validation paths to use it instead of `ErrorResponse`. No
   behavior change beyond response shape; clients adapt.
2. **Add the per-session queue + worker.** In `state.py`:
   `session_mutation_queues`, the `session_worker` async
   function, queue creation in `create_session`, queue closure
   in the close path. In `client_app.py`:
   `_handle_mutate_session` becomes a `send_nowait` with
   `WouldBlock` → `session_busy`. `apply_mutation` itself stays
   sync at this stage — we just reroute the dispatch.
3. **Make `apply_mutation` async.** No new awaits yet; just the
   signature change. Verify all the existing tests still pass.
4. **Move arg-incref to phase 3 (d).** Add the new in-flight
   contribution to invoke / load. Workers gain the
   `in_flight_arg_refs` parameter and the decref-at-commit step.
   Existing tests should be largely unaffected; add tests for the
   within-mutation `[invoke, untrack]` ordering case.
5. **Refactor `CreateCallbackOp` into the new pre-commit
   shape.** Drop `CallbackRecord.state` / `.ready` /
   `_finalize_*` / the old worker shape. Phase 2 awaits bridge
   inline. `lookup_callback` returns a real `ServiceRef`. Drop
   the invoke pre-phase substitution.
6. **Drop now-dead code.** Confirm `_walk_callback_records`,
   `_substitute_callback_records`, the placeholder type — all
   gone. Tighten `wire_escape.unescape`'s `lookup_callback`
   typing.
7. **Update CODE_OVERVIEW.md.**

Each step is its own commit; tests should pass at every step.

## Tests

Most existing 5b/5c tests still apply once we update for the
new async signatures and the (smaller) `CallbackRecord` shape.
Net new test surface:

- **Async pipeline order**: mutations on the same session are
  serialized; mutations on different sessions run concurrently.
- **Backpressure**: queue full produces `session_busy`; client
  can retry.
- **`session_gone` rejection**: mutate after session close
  rejects cleanly.
- **Within-mutation arg-survival**: `[invoke uses S,
  untrack_service S]` in one mutation no longer races; the
  invoke completes successfully because the in-flight
  contribution kept S alive.
- **Within-mutation create+invoke**: `[create_callback at slot
  N, invoke uses ["callback", N]]` works in a single mutation.
- **Pre-commit failure**: simulate a `CreateCallbackOp` whose
  bridge fails (somehow); the whole mutation rejects with
  `callback_setup_failed`; no partial state survives.
- **Pre-commit failure rolls back prior creates**: two creates
  in one mutation, second one fails — first one is also
  released.

## Open questions

- Whether `apply_mutation` should be a method on
  `WorkstationState` or a freestanding function that the
  session worker calls. Today it's a method; the redesign
  doesn't strictly need it to be. Leaving as method for the
  diff to be smaller, but worth revisiting if the body grows.

## Resolved decisions (cross-referenced from above)

- **In-flight arg-ref decref timing**: when the worker exits
  (success or failure or cancellation) — wired via a `finally`
  block in `invoke_worker` / `load_worker`. The args don't need
  to stay alive past dispatch; if the result references one of
  the args, it gets its own retained-event ref via 3-phase
  delivery, so the arg-drop doesn't release the underlying
  service.
- **Pre-commit concurrency**: sequential. Bridge setup is
  workstation-internal and fast; parallelism isn't worth the
  rollback complexity it would add.
- **Callback worker shape**: keep a tiny worker (it owns
  `impl_scope` across `apply_mutation` returns). Use
  `task_group.start()` for the setup handshake.

## Implementation steps

- [x] **1.** Add `MutationRejectedResponse` + reason codes; migrate
  existing stale-ack / conflict / validation rejections.
- [x] Update CODE_OVERVIEW.md (after step 1).
- [x] **2.** Per-session queue + worker; reroute
  `_handle_mutate_session`.
- [x] Update CODE_OVERVIEW.md (after step 2).
- [x] **3.** Make `apply_mutation` async (signature only).
- [x] **4.** Move arg-incref to phase 3 (d); add
  `in_flight_arg_refs` plumbing in invoke / load workers.
- [x] Update CODE_OVERVIEW.md (after step 4).
- [x] **5.** Refactor `CreateCallbackOp` to the pre-commit shape;
  drop `CallbackRecord.state` / `.ready` / `_finalize_*`; codec
  returns real `ServiceRef`s; drop invoke pre-phase.
- [x] Update CODE_OVERVIEW.md (after step 5).
- [x] **6.** Drop dead code; tighten codec typing.
- [x] Update CODE_OVERVIEW.md (final).
