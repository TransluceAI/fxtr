# Stage 1, Chunk 6 — Session write side (the mutation reducer)

Chunk 6 makes `WorkstationSession.userState` writable. Writes flow
through a reducer that batches them via microtask, computes a
JSON-patch + ops bundle against the last server-committed state,
ships a `MutateSessionRequest`, and commits-or-rolls-back. The
session also gains `terminate()` (server-side close) routed through
the same reducer.

This plan is the spec; the high-level chunk description lives in
[`stage1_foundation.md`](./stage1_foundation.md). Both are kept
roughly in sync with the implementation, but a few decisions
shifted during the post-implementation review pass — see
"Post-implementation revisions" below before reading the body as
authoritative.

## Post-implementation revisions

The implementation matches the body of this plan, with these
substantive changes from a review pass after the first
implementation landed:

1. **`RollbackInfo` simplified to `{ kind: "retryable" |
   "permanent"; message: string }`.** The original three-arm
   discriminated union (`rejected` / `connection_lost` /
   `connector_closed`) leaked transport details to consumers
   who only need to know "should I retry?". Reject reasons and
   transport errors are now mapped via internal `classifyReject`
   / `classifyTransportError` helpers. `SessionMutationRejectReason`
   removed from `@fxtr-ui/core`.
2. **`MutationRolledBackError extends Error`** added to
   `@fxtr-ui/core`. Returned-promise mutation methods (today:
   `terminate()`) reject with this; `.info` field carries the
   same `RollbackInfo`.
3. **`terminate()` returns `Promise<void>`**, not `void`.
   Resolves on commit, rejects with `MutationRolledBackError`
   on rollback. Repeated terminate calls reject (caller bug —
   went through the same `assertWritable` gate as writes).
4. **`inflightAtom` dropped.** Nothing reactive needed to observe
   it. The bookkeeping (envelope + updateId + sendPromise +
   rollbacks) consolidated into a single `inflight: Inflight |
   null` field. Two private envelope atoms remain
   (`committedAtom`, `localAtom`).
5. **`pendingFlushPromise: Promise<void> | null`** replaces
   `flushScheduled: boolean`. `detach()`'s drain loop awaits
   this directly rather than guessing about microtask ordering
   with `await Promise.resolve()`.
6. **`applySnapshot` and `applyStateUpdateEvent` are separate
   methods** sharing an `absorbState` helper. The old
   `applyStateUpdate(event, { isSnapshot })` flag is gone.
   Snapshots set `lastProcessedSeq = snapshot.last_state.seq`
   unconditionally (baseline); deltas use strict `>`
   (increment).
7. **`assertWritable(operation)` helper** consolidates the
   three-way write gate (closed / terminating / detaching) and
   is shared by `handleWrite` and `terminate`.
8. **`userState` write fn uses jotai's `set` callback** instead
   of `this.store.set`.

The body below describes the design as planned; where the
revisions above conflict with a body section, the revisions win.

## Scope

In:

- Writable `userState` atom; custom `WritableAtom` whose write
  function intercepts the new value and runs the reducer.
- Three-state internal model: `committedAtom` / `inflightAtom` /
  `localAtom` over a `LocalEnvelope` value (the wire envelope plus
  one synthetic `terminated?: true` field).
- Microtask-coalesced flushes; at most one outstanding
  `MutateSessionRequest` per session.
- Diff + Track/Untrack ops derivation via the chunk-2 helpers
  (`diffStateToPatch`, `collectStubsInState`).
- Optimistic-read merge: `userState`'s public read returns
  `local.user` (i.e., whatever the user just wrote, applied
  immediately).
- Commit / reject / connection-loss paths, with per-write
  `onRollback(info: RollbackInfo)` callbacks accumulated across
  coalesced writes and fired in registration order (oldest first).
  `RollbackInfo` is a discriminated union over the failure source
  (`rejected` / `connection_lost` / `connector_closed`) carrying the
  server's `MutationRejectReason` or the transport error.
- Foreign `state_update` event integration: replace
  `committedAtom`; if `local` has no pending edits, bring it
  along; otherwise leave it (next mutation will stale-ack-reject).
- `session.terminate(finalState?, options?)`: rides the reducer,
  uses a synthetic `terminated: true` local-only field translated
  to a `CloseOp` on the wire.
- Stub creation API moves to the connector:
  `connector.entityRef(shortId)` / `connector.serviceRef(shortId)`.
  Intern maps live on the connector; the session's wire-decode
  delegates to them.

Out (deferred to stage 2):

- `allocateLoad` / `allocateInvoke` / `allocateCallback` stay as
  `NotImplementedError`. The corresponding ops
  (`LoadEntityOp`, `InvokeOp`, `CreateCallbackOp`,
  `CloseCallbackOp`, `LoadEntitySilentOp`, `Get*MetadataOp`,
  `PreserveEventsOp`, `UnpreserveEventsOp`) are not emitted.
- Slot fills (`LoadResultEvent` / `InvokeResultEvent` /
  `CallbackEvent`) continue to be dropped on receipt.
- Path-aware projection helpers (`splitAtom` / `focusAtom`-style)
  for fine-grained subscription. The whole-atom replace+updater
  is the only write API in chunk 6. Projection layer is a
  separate concern, possibly chunk 8.

## Background: what's coming in versus what's going out

Inbound from chunk 5 (no changes needed):
- `SessionSnapshotEvent` → reducer must seed `committedAtom`
  with `last_state.state` (after `unescapeWire`), set
  `inflightAtom = null`, `localAtom = committed`.
- `SessionEventFrame` carrying `state_update` → replace
  `committedAtom`; foreign-event integration rules below.
- `SessionEventFrame` carrying non-state events → still dropped
  in chunk 6 (no slot machinery).

Outbound new in chunk 6:
- `MutateSessionRequest(subscription_id, ack_seq, update_id,
  patch, ops)` issued on flush. `subscription_id` is the
  session's current subscribe subscription_id; `ack_seq` is the
  last seq we acknowledged from a `state_update` event (committed
  state's seq); `update_id` is a per-session monotonic counter.

Server response paths the connection already knows about:
- `MutationCommittedResponse(request_id, subscription_id, seq,
  update_id)` — promote inflight to committed.
- `MutationRejectedResponse(request_id, reason, message)` — drop
  inflight, run rollback.

## Internal state model

The session's three primitive atoms hold values of:

```
type LocalEnvelope = {
  // The user-facing state tree. Stubs at leaves are the same
  // *interned instances* as live elsewhere; reference equality
  // holds within the connector.
  user?: UserState;

  // Synthetic local-only field. Never serialized into the wire
  // patch; if set in `local` but not in `committed`, the diff
  // walker emits a `CloseOp` instead. Server never writes this.
  terminated?: true;
};
```

Atoms:

- `committedAtom: PrimitiveAtom<LocalEnvelope>` — the last
  server-authoritative state we've heard. Replaced wholesale on
  `SessionSnapshotEvent` and on foreign `state_update` events.
- `inflightAtom: PrimitiveAtom<LocalEnvelope | null>` — what we've
  sent in the outstanding `MutateSessionRequest`, or `null` when
  no mutation is in flight. Equal to what `localAtom` was at
  flush time.
- `localAtom: PrimitiveAtom<LocalEnvelope>` — what user code sees.
  May have edits past `inflight`.

Bookkeeping fields (held on the session, not atoms):

- `nextUpdateId: number` — monotonic, starts at 1. Outgoing
  `MutateSessionRequest.update_id`.
- `lastProcessedSeq: number` — the highest `seq` we have *fully
  processed* for this session. Sent as `ack_seq` on outgoing
  mutations.

  **Invariant**: `lastProcessedSeq` must never advance past any
  event the server has sent us but we haven't yet processed.
  Acking past unprocessed work risks the server garbage-
  collecting events we still need (becomes critical in stage 2
  when load/invoke results need preservation; less critical in
  chunk 6 but the same model applies).

  Updated when:
  - A foreign `state_update` event is applied to
    `committedAtom`. Set to `event.seq`.
  - A `MutationCommittedResponse` arrives for our own mutation.
    Set to `response.seq`.
  - A non-state event we don't (yet) handle is dropped. Set to
    `event.seq`. "Decided to do nothing" is still a decision;
    bumping it lets the server garbage-collect events we'll never
    ask for. In chunk 6, every `load_result` / `invoke_result` /
    `callback` / `*_metadata_result` / `*_error` event flows
    through this path because no slots are allocated to consume
    them. In stage 2, the "drop" becomes "record and preserve"
    — but the seq bump still happens after processing.

  Each update site asserts `lastProcessedSeq < incoming.seq`
  before writing. The assert catches the bug class "we acked
  past something we hadn't processed" — should be unreachable as
  long as every event is processed synchronously on arrival
  (which chunk 6 does). If a future chunk introduces async
  processing (e.g. queueing slot-fill work), it must serialize
  with seq updates explicitly; the assert is the tripwire that
  surfaces the violation.

  Events with `seq <= lastProcessedSeq` on arrival are dropped
  with a `console.warn` — they're stale duplicates and
  re-processing has unwanted side effects.

  Initialized from the snapshot: when the first
  `SessionSnapshotEvent` arrives, `lastProcessedSeq =
  last_state.seq` (typically 0 for fresh sessions, per the
  server's synthetic empty-snapshot convention).
- `inflightRollbacks: Array<(info: RollbackInfo) => void>` —
  onRollback callbacks for the mutation currently in flight;
  cleared on commit or rollback.
- `pendingRollbacks: Array<(info: RollbackInfo) => void>` —
  onRollback callbacks for writes since the last flush. Promoted
  to `inflightRollbacks` on send.
- `inflightUpdateId: number | null` — the `update_id` of the
  outstanding mutation, or `null` when nothing is in flight.
  Defensive guard in response handlers: if a response arrives
  whose `update_id` doesn't match (shouldn't happen in practice
  but cheap to check), the handler drops it with a warning.
- `inflightSendPromise: Promise<...> | null` — the live
  `sendRequest` promise for the outstanding mutation. Stored for
  lifecycle clarity (tests can await; reading the session has a
  single place to see "is there work in flight?") and as the
  drain target inside `detach()` (`await this.inflightSendPromise`).
- `dirty: boolean` — set on every write to `localAtom`; cleared
  when we've sent a mutation reflecting the current local. Read
  during foreign-event handling and after commit-promote.
- `flushScheduled: boolean` — guard against re-scheduling the
  microtask. Cleared at the top of the microtask body.
- `detaching: boolean` — set when `session.detach()` begins;
  blocks new writes and prevents any post-commit re-flushes from
  scheduling new work (though existing dirty state still drains
  through the loop in `detach`). Never cleared back to `false` —
  once detaching, terminal `closed` follows.

Derived public atom:

- `userState: WritableAtom<UserState | null, [UserStateWriteArg],
  void>` — read function returns `extractUser(get(localAtom))`,
  write function described below. Lifecycle stays as the existing
  derived read-only atom from chunk 5.

`extractUser(env)`: returns `env.user ?? null`. Same logic as the
chunk-5 `extractUserField` helper; just moves to operate on the
`LocalEnvelope` shape.

## The write API

`userState` becomes
`WritableAtom<UserState | null, [UserStateWrite, UserStateWriteOptions?], void>`.
First arg is the new value (or an updater function); optional
second arg is the options bag.

```
type UserStateValue = UserState | null;
type UserStateUpdater = (prev: UserState | null) => UserState | null;
type UserStateWrite = UserStateValue | UserStateUpdater;

type UserStateWriteOptions = {
  onRollback?: (info: RollbackInfo) => void;
};
```

Call forms:

- `set(session.userState, newValue)` — replace.
- `set(session.userState, prev => next)` — updater.
- `set(session.userState, newValue, { onRollback })` — replace
  with rollback callback.
- `set(session.userState, prev => next, { onRollback })` —
  updater with rollback callback.

The updater form mirrors `setState` / `setAtom`'s convention.
Strictly speaking we don't *need* it (the user can read the
atom and pass a fresh value), and there's no race window since
all writes funnel through the same flush microtask — but it's
the idiom React / jotai users reach for. The write function
discriminates with `typeof === "function"`, no structural
sniffing.

Write function behavior — signature
`(get, set, value: UserStateWrite, options?: UserStateWriteOptions)`:

1. If `localAtom.terminated === true`, `detaching === true`, or
   lifecycle is already `closed`: throw — sessions on the way
   out reject new writes.
2. Resolve `value`: if `typeof value === "function"`, call it with
   `extractUser(get(localAtom))` to get the new user value;
   otherwise use `value` directly.
3. Write `{ ...get(localAtom), user: resolvedValue }` into
   `localAtom`.
4. If `options?.onRollback` was supplied, push it onto
   `pendingRollbacks`.
5. Set `dirty = true`.
6. If `flushScheduled === false`, `queueMicrotask(flush)` and set
   `flushScheduled = true`.

Concretely, this is implemented as a jotai `atom(read, write)`
custom atom; the write function gets the args from `set()` and
does the work above. No wrapper layer.

## The flush microtask

The flush body:

1. Set `flushScheduled = false`.
2. If `inflightAtom !== null`, do nothing — we're waiting for the
   outstanding mutation to ack/reject. The `dirty` flag stays
   set; the post-commit path picks up the work.
3. If `dirty === false`, return — a stale microtask (e.g.,
   queued before a prior flush ran which cleared `dirty`, with no
   subsequent write). Nothing to send.
4. Compute the wire patch + ops:
   - Project each envelope to its wire-visible fields:
     `wireEnvelope(env) = env.user !== undefined ? { user: env.user } : {}`
   - Run `diffStateToPatch(wireEnvelope(committed),
     wireEnvelope(local), escapeOpts)` to produce JSON Patch ops
     rooted at `/user/...` (or `add /user` if local has user and
     committed doesn't).
   - Run `collectStubsInState` on both sides to derive
     Track/Untrack ops (Set diff of entity short_ids and service
     short_ids).
   - If `localAtom.terminated && !committedAtom.terminated`,
     append `CloseOp` to ops.
   - If `committedAtom.terminated`, do not emit further ops; we
     already terminated, this state shouldn't change. This branch
     should be unreachable (writes are blocked once
     `local.terminated` is set, and once `committed.terminated`
     is true the session lifecycle is `closed` which blocks
     writes too). If we hit it, `console.warn` so the bug is
     visible — don't throw; we just stop and return.
5. If `patch.length === 0 && ops.length === 0`, the user wrote
   something that net-no-ops vs committed (typically: write, undo
   write within the same tick). Clear `dirty` and return — don't
   ship empty mutations.
6. Allocate `update_id = nextUpdateId++`. Set
   `inflightUpdateId = update_id`.
7. Set `inflightAtom = localAtom` (the same object — structural
   immutability means no clone needed).
8. Promote `pendingRollbacks` → `inflightRollbacks`; clear
   `pendingRollbacks`.
9. Clear `dirty` *only after* the inflightAtom is set — we've
   captured this state's commit obligation.
10. Send the `MutateSessionRequest` via
    `connection.sendRequest(...)`. Store the returned promise in
    `inflightSendPromise`. Attach `.then` (commit handler) and
    `.catch` (reject / connection-lost handler) — see the response
    handling sections below. The flush itself is sync; we don't
    `await` here.
11. Both response handlers re-entry-guard with the
    `inflightUpdateId` check: if the response's `update_id`
    doesn't match `inflightUpdateId` at handler-time, the
    response is stale (we've already synthetic-rejected from a
    terminal transition or similar) — `console.warn` and drop.

The dirty flag is the cheap "has anything happened?" signal —
set on every write, cleared when we've captured the state into
an inflight mutation (or short-circuited an empty diff). The
empty-diff short-circuit in step 5 is what catches the rarer
"user wrote then undid within the same tick" case, since dirty
alone can't tell that nothing's actually different.

### Patch path scoping

The diff runs against a projected envelope that includes only the
wire-visible fields (`user`):

```
function wireEnvelope(env: LocalEnvelope) {
  return env.user !== undefined ? { user: env.user } : {};
}
const patch = diffStateToPatch(
  wireEnvelope(committed),
  wireEnvelope(local),
  escapeOpts,
);
```

This way the diff helper naturally emits `/user`-rooted patches
without needing a path-prefix tweak — it diffs whatever object
you hand it. The synthetic `terminated` field is stripped before
diffing and translated to a `CloseOp` separately. When
`committed.user` is undefined (fresh session) and
`local.user = {...}`, the diff emits a single
`{op: "add", path: "/user", value: {...}}` op, which is exactly
what the server expects.

### Track/Untrack derivation

`collectStubsInState(local.user)` and
`collectStubsInState(committed.user)` give us
`{entityShortIds, serviceShortIds}` sets for each.

- For each shortId in `local.entityShortIds` not in
  `committed.entityShortIds`: emit `TrackEntityOp({short_id})`.
- For each shortId in `committed.entityShortIds` not in
  `local.entityShortIds`: emit `UntrackEntityOp({short_id})`.
- Same for services.

Order is `Track*` then `Untrack*`. Server doesn't care about
ordering inside the ops list, but a stable order helps tests.

## Mutation response handling

The session subscribes to its own outgoing `MutateSessionRequest`
promise via `.then` / `.catch`. Three resolution paths:

### Commit (`MutationCommittedResponse`)

1. Assert `lastProcessedSeq < response.seq` (see the invariant
   in the bookkeeping section). If the assert fires it indicates
   a real bug; log loudly and drop the response — this is the
   tripwire for the "acked past unprocessed work" class of
   issues.
2. Set `committedAtom = inflightAtom`.
3. Set `inflightAtom = null`.
4. Set `lastProcessedSeq = response.seq`.
5. Clear `inflightRollbacks` (they're no longer applicable —
   commit succeeded).
6. If `committedAtom.terminated === true`, transition lifecycle
   to `closed`. The server's view is now "closed"; no further
   mutations possible.
7. If `dirty === true`, `queueMicrotask(flush)` — there are
   accumulated writes since we sent the in-flight one.

### Reject (`MutationRejectedResponse`)

Per `MutationRejectReason`: `stale_ack` | `conflict` |
`validation_failed` | `callback_setup_failed` | `session_busy` |
`session_gone`.

1. Set `inflightAtom = null`.
2. Set `localAtom = committedAtom` — rollback the optimistic
   view. Note: this clobbers any writes the user made *after* the
   flush (in `pendingRollbacks`). Their `onRollback` callbacks
   also fire (see below). Forward-order rollback gives the chance
   to reapply if desired.
3. Build `info: RollbackInfo = { kind: "rejected", reason:
   response.reason, message: response.message }`. Fire
   `inflightRollbacks` then `pendingRollbacks` with `info`, each
   in registration order. Clear both lists.
4. Set `dirty = false`.
5. If `localAtom.terminated`, it's already been cleared as part
   of step 2 (since `committedAtom` doesn't carry `terminated`).
6. If `reason === "session_gone"`, transition lifecycle to
   `closed` — the server-side session is gone. Otherwise stay in
   current lifecycle state (probably `connected` or `stale`).

### Connection-lost (`ConnectionLostError` / `ConnectionClosedError`)

The connection layer rejects the pending `sendRequest` promise
with one of these when the socket drops mid-request or the
connection is closed.

Treat the same as a `MutationRejectedResponse`:

1. `inflightAtom = null`.
2. `localAtom = committedAtom`.
3. Build `info: RollbackInfo` discriminated on the error type:
   `{ kind: "connection_lost", error }` for `ConnectionLostError`,
   `{ kind: "connector_closed", error }` for `ConnectionClosedError`.
   Fire `inflightRollbacks` then `pendingRollbacks` in
   registration order with `info`.
4. Set `dirty = false`.

The session itself may also be transitioning to `stale` from
the subscription's `onClose("reset")`; that's handled
independently by the chunk-5 FSM. If we end up in `stale`, the
session will resnapshot and `committedAtom` will be refreshed
when the new snapshot arrives.

## Foreign event integration

Server doesn't broadcast `state_update` events for our own
mutations (confirmed: the workstation's commit ack path doesn't
re-emit). So every `state_update` we see is from another client.

Handler for `state_update`:

1. If `event.seq <= lastProcessedSeq`, drop with a
   `console.warn` (stale / duplicate; *don't* bump
   `lastProcessedSeq`, since bumping would either be a no-op or
   move it backward). Otherwise we know `lastProcessedSeq <
   event.seq` — proceed.
2. Decode the event's `state` through `unescapeWire` with the
   connector's intern lookups. Wrap as a `LocalEnvelope` —
   `committed` never carries `terminated`, so just `{user: ...}`.
3. Set `committedAtom = newEnvelope`.
4. Set `lastProcessedSeq = event.seq`.
5. If `inflightAtom === null && !dirty`:
   `localAtom = committedAtom`. Bring the user-facing view along
   to the new server state.
6. Otherwise (inflight pending or local has unsent edits): leave
   `localAtom` alone. The user sees their changes; the next
   mutation will likely fail with `stale_ack` (server's
   ack_seq check) and rollback to the new committed.

Handler for non-state session events (`load_result`,
`invoke_result`, `callback`, metadata results / errors, etc.)
in chunk 6:

1. If `event.seq <= lastProcessedSeq`, log warn and drop without
   bumping (stale duplicate).
2. Otherwise: drop the event's payload (no slot machinery), but
   set `lastProcessedSeq = event.seq`. This signals to the
   server that we've made a decision about this event ("decided
   nothing") and it can garbage-collect.

Stage 2 replaces step 2's "drop" with "route into slot-fill
state and queue any preservation obligation"; the seq bump
remains.

Even though we use the `dirty` flag (not deep equality) to make
this decision, a corner case: at step 4, `dirty === false`
implies we've sent any local edits in a previous inflight — but
that previous inflight should be reflected by `inflightAtom !==
null` (covered by the same branch). If both `inflightAtom` and
`dirty` are false, we genuinely have no local divergence and can
safely snap.

### terminal `state_update`

`event.closed === true` on a foreign `state_update` means someone
else terminated the session. Apply step 2 above (replace
`committedAtom` — including absorbing `closed=true` somehow), but
*also* transition lifecycle to `closed`. The plan today doesn't
add `closed` into our `LocalEnvelope` (only `terminated`
synthetic does); we can either:

- (a) Hold "lifecycle = closed" purely in the lifecycle atom,
  derived from `closed=true` on the most recent state_update.
- (b) Mirror `closed` into the envelope.

(a) is cleaner — the lifecycle atom already exists from chunk 5
and is the public-facing terminal signal. The chunk-5 code
already does this when `event.closed` is true.

## Stub creation API: moving interning to the connector

Today `WorkstationSession` owns `entityStubs` /  `serviceStubs`
maps and supplies them to `unescapeWire` via lookup callbacks.
Move both to `WorkstationConnector`:

```
class WorkstationConnector {
  // existing API...
  entityRef<T = unknown>(shortId: string): EntityRefStub<T>;
  serviceRef<T = unknown>(shortId: string): ServiceRefStub<T>;
}
```

- Both intern on first call: `connector.entityRef("abc")` twice
  returns the same instance.
- The session's wire-decode lookup callbacks delegate to
  `connector.entityRef` / `connector.serviceRef`.
- A wire-decoded ref from session A and a programmatically-minted
  ref from session B for the same `short_id` are now `===`. That
  matches the chunk-2 stub-walk's equality story.
- These methods don't throw on a closed connector — they're pure
  value constructors. Calling them after `connector.close()` is
  fine; the resulting stub is still usable as a value (though
  there'd be no live session to write it to).
- TypeScript variance: the public method signature uses the
  abstract `EntityRefStub<T>` / `ServiceRefStub<T>` from
  `@fxtr-ui/core`; the runtime returns `WorkstationEntityRefStub`
  / `WorkstationServiceRefStub` (which extend the abstracts).

Stubs themselves are tiny (just a `shortId`) and immutable; we
don't GC the intern maps. Future enhancement: drop entries whose
`shortId` is no longer referenced from any live session's state,
but that's an optimization that needs the chunk-2 state walk to
run periodically — not chunk-6 work.

`OpResultStub` / `CallbackInboxStub` are *not* moved to the
connector. Those carry per-session slot identity and stay
session-scoped (per stage-2's design). Chunk 6 doesn't construct
either of them.

## `session.terminate(finalState?, options?)`

Public method on `WorkstationSession`:

```
terminate(
  finalState?: UserState,
  options?: { onRollback?: (info: RollbackInfo) => void },
): void;
```

Behavior:

1. If `localAtom.terminated === true`, no-op (idempotent).
2. If `lifecycle === "closed"`, no-op.
3. Otherwise:
   - Set `localAtom = { user: finalState ?? localAtom.user,
     terminated: true }`.
   - If `options.onRollback`, push onto `pendingRollbacks`.
   - Set `dirty = true`.
   - Schedule flush.

The flush handles the rest: diff with `CloseOp`, send, await.

Consumer ergonomics:

- `session.terminate()` — close, leaving user state as-is.
- `session.terminate({ status: "completed", note: "ok" })` —
  close with a final state write.
- `session.terminate(undefined, { onRollback: () => ... })` —
  close, recover gracefully if rejected.

Composes with other writes in the same tick:

```ts
set(session.userState, foo, { onRollback: r1 });
session.terminate(undefined, { onRollback: r2 });
```

Both updates land on `localAtom` before the flush microtask
runs. The one outgoing mutation carries `patch=[<diff to foo>]`
plus `ops=[<track/untrack...>, CloseOp]`. Both `r1` and `r2` are
in `pendingRollbacks` and promote to `inflightRollbacks` on
send.

## Rollback ordering: forward (registration order)

When a mutation rejects (or connection-lost), the accumulated
callbacks fire in registration order (oldest first).

Rationale: each callback represents a logical undo or a chance
for the caller to react. If callbacks were fired in reverse
order, a "rebase" pattern (re-issue the write with a fresh
ack_seq) would re-apply later writes before earlier ones,
violating user intent. Forward order matches the order the user
issued the writes, so callbacks can rebuild forward.

`inflightRollbacks` were registered before `pendingRollbacks` by
construction (the inflight mutation's writes happened before any
writes that landed during the in-flight window), so firing
inflight-then-pending is equivalent to combined-oldest-first.

Edge: callbacks that throw shouldn't break the chain. Wrap each
call in try/catch with `console.warn`, same pattern as the
chunk-5 `callOnFrame` / `callOnClose` helpers.

### `RollbackInfo` shape

Each callback receives a `RollbackInfo` argument describing the
failure source:

```
type RollbackInfo =
  | {
      kind: "rejected";
      reason: MutationRejectReason;  // server's literal type
      message: string;
    }
  | { kind: "connection_lost"; error: Error }
  | { kind: "connector_closed"; error: Error };
```

The `kind` discriminator lets consumers branch on the failure
mode without sniffing strings. The union is open for extension if
new failure modes appear later (we'd add a new arm with its own
`kind`). Callbacks that don't care about the info can declare
`(_info: RollbackInfo) => void` and ignore it.

## Edge cases and open questions

### Connection drops mid-flight

The connection's `sendRequest` rejects with
`ConnectionLostError`. We treat it as a rejection (rollback +
fire callbacks). The session may also be transitioning to
`stale` simultaneously via its subscription's `onClose("reset")`
— handled by the chunk-5 FSM. After re-subscribe and snapshot,
`committedAtom` is refreshed. If the user wants to retry, they
re-issue the writes.

### `terminate()` while a regular mutation is in flight

The terminate write goes into `localAtom` (sets `terminated:
true`); the existing inflight mutation is unaffected. When that
mutation commits, the post-commit flush picks up the dirty flag
and sends the close mutation. If the in-flight mutation
*rejects*, rollback restores `committedAtom` (which has no
`terminated`), so the terminate's intent is also lost — the
user's `onRollback` fires and they can retry.

### Foreign update arrives while local-dirty

`committedAtom` updates, `localAtom` stays. Next flush computes
diff against the *new* committed. The server's `ack_seq` check
will reject our mutation as `stale_ack` (we send the old
`lastProcessedSeq` snapshot from when the in-flight was sent).
Rollback → committed (which has the foreign update absorbed).
User callbacks fire. They can re-apply with a better base if
they want.

Note that `lastProcessedSeq` advances when absorbing the foreign
event, but the in-flight (if any) was sent with the older value.
That's why it gets the `stale_ack` rejection.

### Connector close while mutation in flight

Owned-connection close: connection's close fans
`ConnectionClosedError` on the pending `sendRequest`. Treat as
rejection (the `.catch` handler runs the rollback path). Session
lifecycle moves to `closed` via the existing subscription
`onClose("connector_closed")` path.

Borrowed-connection close: the underlying connection stays
alive. Mutation can still commit normally. Connector's
`close()` doesn't tear down sessions (per chunk 5 design).
Sessions stay alive until consumer detaches them or the
underlying connection itself closes.

### `detach()` while mutation in flight — drain semantics

`detach()` becomes async (`Promise<void>` return) — this reverses
the chunk-5 fire-and-forget decision. The reason: with writes
landing in chunk 6, a sync detach would leave pending mutations
in an ambiguous state ("did my write commit?"). By draining
first, the consumer awaits a deterministic outcome.

The drain:

1. If already detaching or closed, return resolved.
2. Set `detaching = true`. This blocks new user writes through
   the write function (alongside `local.terminated` and
   `lifecycle === "closed"` — all three gate the write fn).
3. Loop while there's any pending work:
   - If `inflightSendPromise !== null`: await it (with
     `.catch(() => {})`; we don't care about the rejection
     itself here — the regular response handler runs the
     commit-or-reject path, which is what we want).
   - Else if `dirty === true`: yield once
     (`await Promise.resolve()`) for the pending flush
     microtask to fire. It'll send a mutation and set
     `inflightSendPromise`, which the next loop iteration awaits.
4. After the loop, every write the user issued has been
   resolved on the server (committed) or rolled back with its
   callback fired. Send the unsubscribe via `handle.close()`.
   The chunk-5 onClose("unsubscribed") handler flips lifecycle
   to `closed`.

Wire ordering gives us the equivalent guarantee to "wait for the
unsubscribe ack": our pending mutation is on the same connection,
sent before any subsequent unsub, so by the time the mutation's
response arrives our drain completes. We don't need to await the
unsub ack itself.

### Other terminal transitions while a mutation is in flight

`connector.close()` (owned mode), foreign terminal
`state_update.closed=true`, and `terminate()` commit are all
*not* user-initiated drains. They don't await anything. When
they happen while `inflightAtom !== null`:

- Lifecycle flips to `closed` via the chunk-5 handlers.
- The in-flight mutation's response eventually arrives.
- The response handler runs the standard commit/reject path:
  atoms update on commit; rollbacks fire on reject. **A closed
  lifecycle is not a reason to suppress these** — the user
  registered the callback because they want to know the outcome
  of their write, and the outcome is independent of why the
  session ended up closed.
- For owned-mode connector close specifically, the connection's
  `close()` rejects the pending `sendRequest` with
  `ConnectionClosedError` directly, which routes into the
  reject path. The rollback fires; consumer learns the write
  didn't commit.

### A `MutationCommittedResponse` arrives for an unknown `update_id`

Shouldn't happen — server only acks our own requests, and our
flush always sets `inflightUpdateId` before sending. The
`inflightUpdateId` guard is defensive: if a response's
`update_id` doesn't match, log a warning and drop. The same
guard catches any truly unexpected late-arriving response (e.g.,
a buggy server, a race we haven't anticipated).

### Rapid terminate-then-write

```ts
session.terminate();
set(session.userState, foo); // throws — `local.terminated` is set
```

Write API throws synchronously. Behavior is documented; consumers
who want to "terminate with final state" use `terminate(foo)`.

## Public API surface changes

`@fxtr-ui/core/src/session.ts`:

- `Session.userState` type changes from `Atom<UserState | null>`
  to `WritableAtom<UserState | null, [UserStateWrite,
  UserStateWriteOptions?], void>`. Strictly additive — existing
  read-only consumers still work. `UserStateWrite` /
  `UserStateWriteOptions` / `RollbackInfo` are exported.
- `Session.detach()` return type changes from `void` to
  `Promise<void>` — reverses the chunk-5 fire-and-forget
  decision. Drains pending mutations before unsubscribing.
- TODO comment for `terminate()` on the abstract base — still
  workstation-only.

`@fxtr-ui/connector-workstation/src/connector.ts`:

- New methods: `entityRef<T>(shortId)`, `serviceRef<T>(shortId)`.
- New internal: connector-level intern maps.

`@fxtr-ui/connector-workstation/src/session.ts`:

- `userState` becomes the custom writable atom described above.
- `lifecycle` unchanged.
- `detach()` becomes `async detach(): Promise<void>` with drain
  semantics.
- New: `terminate(finalState?, options?): void`.
- `entityStubs` / `serviceStubs` maps move out; session uses
  connector's lookups.
- New private: `committedAtom` / `inflightAtom` / `localAtom`,
  bookkeeping fields, flush microtask body, response handlers.

## Tests to write

In a new `tests/mutation.test.ts` (or extension of
`session.test.ts`):

### Happy-path mutation

- Write `set(userState, X)` → flush microtask → mutation sent
  with patch / ops / ack_seq → `MutationCommittedResponse` →
  committed updated, lifecycle stays `connected`.

### Microtask coalescing

- Multiple sync writes in one tick → single mutation containing
  the combined diff.

### Pending while inflight

- Write → flush → mutation in flight → user writes again →
  `localAtom` advances, `dirty` set, no new mutation sent → ack
  arrives → flush re-runs, sends mutation 2 with the additional
  diff.

### Optimistic read

- Write → before ack, `useAtomValue(userState)` returns the new
  value (not committed).

### Reject path

- Write `{ value: X, onRollback: r }` → reject → `localAtom`
  reverts to committed, `r` fires, user can re-write.

### Forward-order rollback

- Two writes `A` then `B` with `onRollback: rA, rB` → reject →
  `rA` fires before `rB`.

### Connection-lost mid-flight

- Write → flush → simulate socket drop → `ConnectionLostError`
  triggers rollback path; `dirty` clears; later state_update
  (after resubscribe) replaces committed cleanly.

### Foreign update absorbs

- Snapshot loads → no local edits → foreign `state_update` →
  `committedAtom` + `localAtom` both update.
- Snapshot loads → local edit → foreign `state_update` →
  `committedAtom` updates, `localAtom` stays; in-flight (if any)
  rejects with stale_ack; rollback to new committed.

### Track/Untrack derivation

- Write a state containing `connector.entityRef("a")` →
  `TrackEntityOp({short_id: "a"})` in the mutation's ops.
- Remove the ref → `UntrackEntityOp({short_id: "a"})`.
- Same for services.
- Adding and removing a ref in the same coalesced batch nets to
  no op.

### Stub interning at the connector

- `connector.entityRef("a") === connector.entityRef("a")`.
- A wire-decoded `["entity", "a"]` from a session snapshot
  produces the same instance as `connector.entityRef("a")`.
- Two sessions share the same intern: stubs from snapshots in
  session A and session B for the same short_id collapse.

### Termination

- `terminate()` → flush sends mutation with `[CloseOp]` and no
  patch → commit → lifecycle `closed`.
- `terminate(finalState)` → mutation carries both patch and
  CloseOp.
- `terminate()` then `set(userState, X)` → write throws.
- `terminate()` rejected → `local.terminated` cleared, session
  stays connected, onRollback fires, future writes allowed.
- `terminate()` while mutation in flight → queued; commits in
  sequence after first.

### NotImplementedError still in place

- `allocateLoad` / `allocateInvoke` / `allocateCallback` still
  throw `NotImplementedError`. (These pass through unchanged
  from chunk 5.)

## Implementation steps

1. **Connector intern + ref API.** Move `entityStubs` /
   `serviceStubs` maps from `WorkstationSession` to
   `WorkstationConnector`. Add public `entityRef` / `serviceRef`
   methods. Adjust the session to delegate.
2. **Update CODE_OVERVIEW.md** for the connector / stub changes.
3. **`LocalEnvelope` type + three-atom internal state.** Refactor
   the session's userState handling: introduce `committedAtom` /
   `inflightAtom` / `localAtom`. `userState` public read derived
   from `localAtom`. No write surface yet.
4. **Custom writable userState atom.** Replace the derived
   read-only atom with a custom `atom(read, write)`.
   Implement the write function (resolve updater, push to
   `localAtom`, push to `pendingRollbacks`, schedule flush).
5. **Flush microtask + sendRequest wiring.** Implement the flush
   body. Diff + ops bundle (Track/Untrack only for stage 1; no
   CloseOp yet). Inflight tracking.
6. **Commit + reject + connection-lost handling.** Wire up the
   sendRequest promise to commit/reject paths. Forward-order
   rollback. Re-flush if `dirty`.
7. **Foreign-update integration.** Update the chunk-5
   `applyEvent` path to use the new committed atom; absorb into
   `localAtom` when no local divergence.
8. **`terminate()` method.** Set the synthetic `terminated`
   field; flush emits `CloseOp`. Write-rejection while
   terminated. Post-commit lifecycle = closed.
9. **Tests.** Cover the matrix above. `MockWebSocket`-driven;
   real-workstation coverage is chunk 7.
10. **Update CODE_OVERVIEW.md** for the final state.

## What chunks 7+ / stage 2 add

- **Chunk 7**: stage-1 integration test against a real demo
  workstation server. Confirms the mutation cycle round-trips on
  the wire, including reset/reconnect during pending mutations.
- **Chunk 8** (proposed): path-aware projection helpers
  (`splitAtom` / `focusAtom`-style) so React components can
  subscribe to sub-trees of `userState` without re-rendering on
  unrelated changes. Build on chunk 6's whole-atom write
  semantics.
- **Stage 2**: slot allocation. `allocateLoad` / `allocateInvoke`
  / `allocateCallback` are implemented; the corresponding ops
  (`LoadEntityOp` etc.) are emitted from the diff walk;
  `OpResultStub` / `CallbackInboxStub` gain consumer-facing
  atoms backed by the slot-fill map; `PreserveEventsOp` /
  `UnpreserveEventsOp` flow from the state walk.
