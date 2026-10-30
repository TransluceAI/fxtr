# Stage 2 — Entity loading + slot mechanics

Adds the slot machinery on top of stage 1. After this, you can
allocate a load against an `EntityRefStub`, store the slot into
state, see its result flow back, have it be preserved on the
workstation server, and survive rollbacks / reconnects without
losing fills or drifting from the server's view of what's
preserved.

Scope: only **entity loads**. `allocateInvoke` /
`allocateCallback` still throw `NotImplementedError`; they land
in stages 5 and 6 respectively. The slot machinery built here
is shaped so invoke/callback fit additively — the spec
discriminator is open for future kinds, the preserve-events
machinery applies as-is, etc.

## What gets built

- `WorkstationOpResultStub` concrete class with three inner
  atoms (`result`, `committed`, `done`) and a session backref
  for cross-session validation.
- `Session.allocateLoad(stub)` — synchronous slot allocation
  returning an interned `WorkstationOpResultStub`.
- Wire codec extensions for `["op_result", slot]` and
  `["broken_op_result", slot]` (see "Broken stubs" below).
- Reducer extensions:
  - `LoadEntityOp` derivation from state diff.
  - Slot dedup via the per-envelope `loadDedup` index,
    rebuilt by the flush walk.
  - `LoadResultEvent` / `LoadErrorEvent` → `slotFills`
    (eager decode through `unescapeWire`; decode failure
    synthesizes an errored fill).
  - Re-derived `committedPreservedSeqs` mirror (see
    "Preserve-set invariant" below).
  - `committedSlotSet` index backing the stub's `committed`
    atom.
  - `brokenSlots` set + broken-stub detection during the
    flush walk.
  - `slotFills` purge driven off the re-derive.
  - `localSlotCounter` advance rules (`max(local,
    state_update.slot_counter)` + `+1` on each allocate).
  - Hard tripwire on out-of-range wire slots.

## Data model

### Session-level (not in envelope)

- `opResultStubs: Map<slot, WorkstationOpResultStub>` — interning.
  Kept distinct from a future `callbackInboxStubs` map (stage 6) —
  slot integers are session-wide unique, but mixing kinds in one
  map would force `allocateLoad`'s dedup lookup to widen past
  `OpResultStub<T>` and lose the type guarantee.
  **Append-only on mint.** Stubs are stateless wrappers around
  `(slot, session)`; both `.result` and `.committed` read from
  session-level maps below, so a stale interning entry from a
  rolled-back optimistic allocation is observationally
  indistinguishable from a freshly minted stub at the same
  slot. No eviction needed.
- `pendingSlotSpecs: Map<slot, OpSpec>` — specs for slots
  whose op hasn't been emitted yet. Entry added on `allocateX`;
  **the whole map is wiped at the end of each flush**
  (whatever was emitted is now in flight; whatever wasn't
  emitted was never written into local state and is unreachable
  from any rolled-back-or-not local view). Also wiped on
  reject (already-wiped under steady state, but defensive in
  case of overlapping operations). `OpSpec` is a discriminated
  union; stage 2 only ships `{ kind: "load"; shortId: string }`,
  with a TODO seam for `"invoke"` / `"callback"`.
- `brokenSlots: Set<number>` — slot integers permanently
  flagged as broken during a flush walk. Once flagged, never
  unflagged. Stub atoms for these slots report `errored`.
  See "Broken stubs" below.
- `slotFills: Map<slot, FillRecord>` — accumulator. Appended on
  `LoadResultEvent` / `LoadErrorEvent` (eager decode). Purged
  after each committed state change for entries the server has
  also dropped (seq ≤ ack_seq but not preserved).
- `committedPreservedSeqs: Set<number>` — mirror of server's
  `preserved_events` sidecar. **Re-derived** on every committed
  state change.
- `committedSlotSet: Set<number>` — slots referenced in current
  committed user state. Backs `OpResultStub.committed`. Rebuilt
  on every committed change (same walk that powers the mirror
  re-derive).
- `localSlotCounter: number` — monotonic.

### Envelope (rolls back atomically)

```
SessionStateEnvelope {
  user?: UserState
  terminated?: true
  loadDedup: ReadonlyMap<shortId, slot>   // for fast allocateLoad dedup
}
```

`loadDedup` is the only addition to the chunk-6 envelope. It's
updated by `allocateLoad` (the allocator writes through the
local atom, so the existing reducer plumbing handles
inflight/commit/reject snapshots automatically).

## Preserve-set invariant (no-drift design)

The server's `preserved_events` sidecar is session-global —
foreign mutators add and remove entries we don't see directly
in any frame other than snapshots. To avoid drift we **never
rely on tracking our own preserve/unpreserve ops**; instead, on
every committed state change we re-derive the mirror from
observable state:

```
On (our commit | foreign state_update | snapshot):
  committedPreservedSeqs = {
    slotFills[s].seq
    for s in (slots referenced in new committed user state)
    if slotFills[s] exists AND slotFills[s].seq <= new committed.ack_seq
  }
```

This is just the protocol-compliance invariant (*"a well-behaved
mutator preserves exactly the in-state-and-acked fills"*)
applied as a derivation rule on our side. As long as every
mutator follows the protocol, our mirror stays in sync.

Consequences:

1. The flush's preserve-diff compares
   `desiredPreservedSeqs` (re-derived against **local**) vs
   `committedPreservedSeqs`. Difference → `PreserveEventsOp` /
   `UnpreserveEventsOp` on the outgoing mutation.
2. `slotFills` purge piggybacks: after re-deriving, drop any
   `slotFills` entry whose seq is `<= committed.ack_seq` and not
   in the new mirror — the server dropped them, so do we.

### Snapshot path

On `SessionSnapshotEvent`, the server's the authority:

- `slotFills` rebuilds from `preserved_events + subsequent_events`
  (decode each payload via `unescapeWire`).
- `committedPreservedSeqs = { e.seq for e in preserved_events }`.
- `committedSlotSet` rebuilds from a walk of `last_state.state`'s
  decoded user envelope.

### Drift canary

If a foreign mutator violates the protocol (e.g. preserves
something not reachable from state), our next
`PreserveEventsOp` / `UnpreserveEventsOp` may be rejected by
the server with `validation_failed`. Log loudly; this surfaces
the protocol violation rather than swallowing it.

## Slot dedup

`allocateLoad(refstub)`:

1. Read `localEnvelope.loadDedup[refstub.shortId]`. If present,
   return `slotStubs[that slot]`.
2. Else mint slot `N = nextSlotCounter()`; construct
   `WorkstationOpResultStub(N, session)`; intern in `slotStubs`;
   write `pendingSlotSpecs[N] = { kind: "load", shortId: ... }`;
   write `loadDedup[shortId] = N` through the local atom.
   Return the stub.

`loadDedup` updates via two paths:

- **Incrementally on `allocateLoad`** (above), so a same-tick
  re-allocate hits the cache.
- **Rebuilt from local user state on the flush walk**, so any
  staleness (slot removed from state but not from `loadDedup`)
  is corrected before commit. The reconciled `loadDedup` is
  what lands in the inflight envelope, and therefore in the
  committed envelope on success.

Cases this handles correctly:

- Two components both call `allocateLoad(X)` before any flush
  → both hit the same `loadDedup` entry → same stub returned;
  state diff sees one new slot; one `LoadEntityOp` emitted.
- User optimistically removes the slot ref and re-allocates X
  in the same tick: the removal doesn't touch `loadDedup` (only
  `allocateLoad` writes it incrementally), so the re-allocate
  finds the same slot and returns the same stub. The flush
  walk sees the slot back in user state and keeps the
  `loadDedup` entry.
- User removes the slot ref and *doesn't* re-allocate before
  flush: the flush walk doesn't find the slot in user state,
  so the reconciled `loadDedup` (in inflight) drops the entry.
  Commit promotes that to committed. A future
  `allocateLoad(X)` mints a fresh slot.
- Flush sends an unpreserve, then a later micro-tick re-adds
  the same stub before the previous flush commits: dedup still
  has X→S in local, so the re-add reuses the same stub. The
  inflight (sent earlier) had `loadDedup` without X (because
  the slot wasn't in local user state at flush time), but
  local diverged after that. On commit, committed
  takes inflight's `loadDedup` (no X) — but local has X→S, and
  the next flush walk will reconcile, keeping it.
- Reject of a mutation that introduced new slots: local rolls
  back to committed → `loadDedup` rolls back too →
  speculative stubs are unreachable through dedup; a fresh
  `allocateLoad(X)` mints a new slot.

## Stub public surface

```
class WorkstationOpResultStub<T> extends OpResultStub<T> {
  slot: number
  session: WorkstationSession       // backref; for cross-session validation
                                    //   at flush walk time
  result:    Atom<{ status: "pending" }
                 | { status: "filled"; value: T }
                 | { status: "errored"; message: string }>
  committed: Atom<boolean>          // slot is in committed user state
  done:      Atom<boolean>          // result.status !== "pending"
}
```

- `result` is derived from `session.slotFillsAtom` indexed by
  `slot`, **with `session.brokenSlotsAtom` taking precedence**:
  if the slot is in `brokenSlots`, `result.status = "errored"`
  with a synthesized message regardless of `slotFills`.
- `committed` is derived from `session.committedSlotSetAtom`
  indexed by `slot`.
- `done` is derived from `result`.
- The type parameter `T` is best-effort TS-level only — see
  "Open questions" below.

The constructor is not part of the public surface — consumers
go through `session.allocateLoad` (the only entry point in
stage 2) or wire-decode via `lookupOpResult`. We don't strictly
prevent direct construction (TS can't hide the class), but
manually-minted stubs end up flagged broken at the next flush
walk (no `pendingSlotSpec`, slot may also fail the
`localSlotCounter` bound).

## Broken stubs

A stub is *broken* when it appears in local user state but
doesn't correspond to a legitimate slot from this session's
perspective. Two structurally different cases, distinguished
by whether we own the stub's identity:

### Cross-session stubs (we don't own the stub)

The stub's `session` backref points at a different session
than ours. Its atoms read from another session's maps. We
can't make it report errored, so we **substitute** it during
the flush walk with a session-scoped *dead-sentinel stub*:

- Each `WorkstationSession` mints a single dead-sentinel
  `WorkstationOpResultStub` instance with `slot = 0` (server
  rejects slot=0 in real ops, so it's safely unused as a
  sentinel) interned at session construction.
- The dead-sentinel's `result` atom always reports `errored`
  with a "dead stub (cross-session or unrecoverable)" message.
- At the flush walk, every leaf whose stub is cross-session
  is replaced in local user state with the dead-sentinel.
  The substituted local is written back to the local atom
  before computing the diff (one write, in-flush, doesn't
  re-schedule).
- Encoded in the patch as `["broken_op_result", 0]`. Other
  clients decoding `["broken_op_result", 0]` resolve to their
  own session's dead-sentinel.

If the flush rejects, local rolls back to committed — the
cross-session stub may return. The user has to remove it
themselves; we don't try to "remember" cross-session
violations across rollbacks (no good local position for that).
Next flush would re-substitute.

### Our-but-broken slots (slot is ours but unhealthy)

The stub belongs to this session but the slot is unhealthy:

1. **Slot in local user state, not in committed user state,
   and no `pendingSlotSpec[slot]`** — the slot used to be valid
   (foreign-decoded into committed) and got removed by a
   foreign mutator while we still held a reference, OR the
   user manually constructed a `WorkstationOpResultStub`
   bypassing `allocateLoad`.
2. **Slot integer `> localSlotCounter`** seen on the encode
   side. The wire-decode path's tripwire catches incoming
   bad slots; this is the symmetric guard on outgoing.

Here we *don't* substitute — the stub instance is ours and its
atoms can read `brokenSlots`. We just:

- `brokenSlots.add(slot)` permanently.
- Encode at the slot's path as `["broken_op_result", slot]`
  (real slot integer).
- Emit no `LoadEntityOp` (or other op) for the slot.
- Exclude from the preserve-set re-derive walk.

### Wire encoding (shared)

Both cases share the `["broken_op_result", slot:number]`
wire tag, distinguished by slot value:
- `slot = 0` ⇒ session's dead-sentinel (cross-session
  substitution).
- `slot > 0` ⇒ real (now-broken) slot from this session's
  namespace.

On decode:
- `["broken_op_result", 0]` resolves to the receiver's
  dead-sentinel.
- `["broken_op_result", slot]` (slot > 0) decodes via
  `lookupOpResult(slot)` *and* adds `slot` to `brokenSlots`,
  so the brokenness propagates to any client seeing the
  state.

### Effects

- No `LoadEntityOp` (or other op) emitted for broken slots.
- `result` atom reports `errored`. For our-but-broken slots,
  via `brokenSlots` lookup. For cross-session substitutions,
  via the dead-sentinel's static errored behavior.
- `committed` atom reflects whether the broken tag is in
  committed state (truthful).
- `slotFills` / `committedPreservedSeqs` exclude broken slots
  from the preserve set walk.

### What this lets us catch

Broken-stub handling is mostly a defensive surface, but it
makes user-code bugs (manually constructed stubs, cross-session
smuggling) and protocol-violating foreign mutators
(unilaterally unpreserving fills we still reference) surface
as observable errored fills rather than silently hanging at
`pending` or breaking the diff invariants.

## Worked-through scenarios

### Scenario A: fill arrives for a slot we already optimistically removed

State before: committed has slot S, local has no S (user already
deleted it; mutation pending). `committedPreservedSeqs = {}`.
`slotFills` empty.

1. Fill `F` for slot S arrives. `slotFills[S] = (seq=F.seq,
   value=…)`. `lastProcessedSeq` bumps.
2. User's removal mutation reaches flush:
   - `desiredPreservedSeqs` from local: `{}` (no S in local).
   - Diff vs `committedPreservedSeqs ({})`: no preserve/unpreserve.
   - Mutation goes out with just the patch.
3a. Commit → re-derive against new committed (no S) → `{}`.
    Purge drops `slotFills[S]` (seq ≤ ack_seq, not preserved).
    Server-side: F drops from `event_log` because ack_seq
    advanced past it.
3b. Reject → local rolls back, S restored to local user state.
    `slotFills[S]` is still present. Next flush:
    `desiredPreservedSeqs = {F.seq}`, mirror `{}` → emit
    `PreserveEventsOp([F.seq])`. Commit; re-derive picks it up.

### Scenario B: preserved fill, user removes the slot

State before: committed has slot S; `slotFills[S] = (F.seq,
…)`; `committedPreservedSeqs = {F.seq}`.

1. User removes S from local.
2. Flush: `desired = {}`, mirror `{F.seq}` → emit
   `UnpreserveEventsOp([F.seq])` alongside the patch.
3a. Commit → re-derive against new committed (no S) → `{}`.
    Purge drops `slotFills[S]`.
3b. Reject → mirror stays at `{F.seq}`; `slotFills` unchanged.
    Local restored to having S. Next flush diff: `{F.seq}` vs
    `{F.seq}` → no preserve/unpreserve op.

### Scenario C: reconnect mid-flight

State before: committed has slot S preserved; local has S
removed; unpreserve mutation in flight.

1. Socket drops mid-mutation.
2. Connection layer rejects the pending request with
   `ConnectionLostError`. Reducer rolls back local to
   committed (S restored). `pendingSlotSpecs` cleared.
3. Re-register on reset. Snapshot arrives:
   - `slotFills` rebuilds from `preserved_events`.
   - `committedPreservedSeqs = {F.seq}`.
   - `committedSlotSet` rebuilds.
4. State is consistent. User can re-issue the removal.

## Implementation chunks

Each chunk is one reviewable PR; ordering reflects review flow,
not "feature-gated" boundaries. All correctness logic lives in
chunk 3; chunk 4 just adds the edge-case test suite.

### Chunk 1 — Wire codec + concrete stub + state walks ✅

- [x] Add `op_result` and `broken_op_result` to `WireValue` and
  the list-escape codec (`escape.ts`):
  `["op_result", slot:number]`,
  `["broken_op_result", slot:number]`. Round-trip and
  error-path unit tests.
- [x] `WorkstationOpResultStub<T>` concrete class (`stubs.ts`):
  fields `slot`, `session`, `result`, `committed`, `done`.
  Construction takes `(slot, session)`; the atoms are derived
  against the session's `slotFillsAtom`,
  `committedSlotSetAtom`, and `brokenSlotsAtom` (placeholder
  atoms introduced here, populated in chunk 3).
- [x] Each session mints and interns a single
  *dead-sentinel* `WorkstationOpResultStub` (slot=0) at
  construction. Its `result` atom is hardwired to errored
  (independent of `slotFills` / `brokenSlots`).
- [x] Extend `collectStubsInState` to surface slot integers
  from `OpResultStub` leaves; extend `diffStateToPatch` to
  emit `["op_result", slot]` for stub leaves by default.
  Equality rule for `OpResultStub` in the diff: by slot
  integer.
- [x] Extend `unescapeWire`'s lookup callbacks with
  `lookupOpResult(slot, broken)`; thread through the session.
  Decode rules:
  - `["op_result", slot]` (slot > 0) → `slotStubs[slot]` or
    mint fresh.
  - `["broken_op_result", 0]` → the session's dead-sentinel.
  - `["broken_op_result", slot]` (slot > 0) →
    `slotStubs[slot]` or mint; **TODO chunk 3**: also add slot
    to `brokenSlots` (chunk 1 leaves this as a no-op since the
    placeholder set stays empty).
- [x] Tests for the walks with both tags + dead-sentinel.
- [x] Update `frontend_lib/CODE_OVERVIEW.md`.

No allocation API, no fills, no preserve, no broken-stub
detection on encode (that's chunk 3 — chunk 1 just wires the
codec, stubs, and dead-sentinel infra).

### Chunk 2 — Allocator + counter + dedup + spec map ✅

- [x] `localSlotCounter` on `WorkstationSession`. Bump rules:
  `max(local, state_update.slot_counter)` on every
  state_update absorb (and `last_state.slot_counter` on every
  snapshot), `+1` on every `allocateX`. Counter advance runs
  *before* state decode so the `lookupOpResult` tripwire sees
  correct values.
- [x] `pendingSlotSpecs` session-level map + `OpSpec`
  discriminated-union type (stage 2 ships `"load"`; TODO seam
  in `"invoke"` / `"callback"` as the union grows).
- [x] `LocalEnvelope` extended with
  `loadDedup: ReadonlyMap<string, number>`; default empty;
  preserved across snapshot / state_update absorbs (chunk-2
  limitation — chunk 3 will rebuild from the flush walk).
- [x] Implement `allocateLoad(refstub)`: dedup via the local
  envelope's `loadDedup`; on miss, mint slot, intern stub,
  record spec, write `loadDedup` directly through the
  `localAtom` (bypassing the public write API — allocate
  alone doesn't schedule a flush).
- [x] Tripwire: in `lookupOpResult(slot, broken)`, if `slot >
  localSlotCounter`, throw loudly (invariant violation —
  server announced a slot we haven't seen a state-update for).
- [x] Unit tests for the counter rules, optimistic dedup
  before commit, dedup across optimistic remove +
  re-allocate, tripwire firing.
- [x] Update `frontend_lib/CODE_OVERVIEW.md`.

Still no wire-op emission for loads — that comes in chunk 3.

### Chunk 3 — Reducer integration: ops, fills, preserve diff, mirror, purge, broken-stub detection ✅

The substantive chunk. The reducer fully handles slot lifecycle
end-to-end, including all rollback / foreign-update edge cases.
Implemented correctly; only smoke-tested the happy path (edge
cases in chunk 4).

- [x] **Single flush walk** over local user state, producing:
  - Cross-session-stub substitution: any stub whose
    `session` differs from ours is replaced in-walk with our
    dead-sentinel. If any substitutions happened, the
    rewritten user-state is written back to the local atom
    via a direct `store.set(localAtom, …)` (in-flush; does
    NOT call `scheduleFlush`).
  - The JSON patch (computed against the substituted local).
  - The reconciled `loadDedup` (slots-in-state grouped by
    their stub's load spec → shortId; excludes the
    dead-sentinel).
  - Per-slot classification: `legitimate` (in committed OR
    has `pendingSlotSpec`), `new-with-spec` (emit
    LoadEntityOp), or `broken` (slot > 0 but no spec and not
    in committed, OR `slot > localSlotCounter`).
  - Slots in `desiredPreservedSeqs` (slots-in-state with
    known fills, seq ≤ ack_seq, *excluding* broken slots and
    the dead-sentinel).
- [x] State-diff → `LoadEntityOp` derivation. For each
  `new-with-spec` slot, emit `LoadEntityOp(slot, shortId)`
  using `pendingSlotSpecs[slot]`. Encode the stub at its path
  as `["op_result", slot]`.
- [x] Broken-stub encoding for our-but-broken slots. For each
  `broken` slot: `brokenSlots.add(slot)`; encode at the slot's
  path as `["broken_op_result", slot]` in the patch (via
  `encodeOpResult` consulting `brokenSlots`); emit no
  `LoadEntityOp`; exclude from the preserve-set walk.
- [x] Dead-sentinel encoding for cross-session-substituted
  stubs (already swapped in by the walk): natural encoding via
  the existing rule emits `["broken_op_result", 0]` at the
  substituted path.
- [x] `pendingSlotSpecs` wiped at the *end* of the flush
  (post-emission).
- [x] `LoadResultEvent` / `LoadErrorEvent` handling: eager
  decode through `unescapeWire`; store in `slotFills`. Decode
  failure (`unescapeWire` throws) → synthesize an errored
  fill (`{ seq, kind: "errored", message: "decode failed: …" }`)
  so the stub's `result` atom transitions to errored. Bump
  `lastProcessedSeq` (existing rule). Plus a fill-event slot
  > `localSlotCounter` tripwire.
- [x] `committedPreservedSeqs` re-derived on every committed
  state change (our commit, foreign state_update, snapshot)
  via `reindexCommittedState`. The same call populates
  `committedSlotSet`. Broken slots are excluded by virtue of
  being outside committed-state's stub walk.
- [x] Preserve-set diff at flush time: `desired` (computed
  against local-after-walk, excluding broken slots) vs
  `committedPreservedSeqs` → emit `PreserveEventsOp` /
  `UnpreserveEventsOp`.
- [x] `slotFills` purge on each re-derive: drop entries whose
  seq is `<= committedAckSeq` but no longer preserved.
- [x] Reject path: clear `pendingSlotSpecs`. (Local rolls back
  via existing chunk-6 plumbing; `loadDedup` rolls back as
  part of the envelope; `brokenSlots` is permanent so it
  stays.)
- [x] Loud `console.error` on any `validation_failed` reject
  reason. Server-side mutation-pipeline failures (unknown
  short_id, non-monotonic slot, preserve-set drift, etc.) can't
  recover programmatically; the reducer can't distinguish the
  multi-mutator-drift case from other validation failures
  cheaply, so we just surface all of them loudly.
- [x] Smoke test through `MockWebSocket`: allocate, fill
  arrives, result resolves, slot removed, unpreserve emitted,
  `slotFills` purged on commit. (6 tests in
  `slot_reducer.test.ts`.)

### Chunk 4 — Edge-case test suite ✅

All edge-case logic was implemented in chunk 3 (plus one missed
chunk-3 gap: `lookupOpResult(broken=true)` now properly writes
to `brokenSlots`); this chunk adds focused tests via
`MockWebSocket`. 15 new tests in `slot_reducer.test.ts` on top
of the 6 chunk-3 smoke tests.

- [x] Scenario A (fill arrives after optimistic remove): both
  commit and reject sub-cases.
- [x] Scenario B (preserved fill, then removal): both
  sub-cases (commit purges slotFills; reject leaves mirror
  intact and the next flush is a no-op).
- [x] Scenario C (reconnect mid-flight): snapshot rebuilds
  `slotFills` + `committedPreservedSeqs` from
  `preserved_events` + `subsequent_events`; replaces stale
  local fills with the server's authoritative set.
- [x] Slot dedup mid-flight: two `allocateLoad(X)` calls
  before commit return the same stub; a single
  `LoadEntityOp` is emitted.
- [x] Foreign state_update reconciliation: preserving the
  slot we reference; foreign state_update where our slot
  disappears (mirror re-derive drops the seq; slotFills
  purges via the reindex hook).
- [x] Cross-session-stub flow: smuggled stub from another
  session is substituted in-place; patch carries
  `["broken_op_result", 0]`; no LoadEntityOp emitted; the
  user's reactive read returns the dead-sentinel.
- [x] Our-but-broken-slot flow: stage covered through
  combined reject-and-rebroadcast scenario showing the
  rollback path.
- [x] Wire decode of `["broken_op_result", slot]` (slot > 0):
  adds slot to `brokenSlots`; the stub's atoms report
  errored. Decoding `["broken_op_result", 0]` resolves to the
  receiver's dead-sentinel.
- [x] Fill decode-error path: synthetic malformed payload
  yields an errored fill, reducer doesn't throw.
- [x] `validation_failed` reject canary: server-rejected
  mutation logs a loud `console.error` carrying the message.
- [x] Fill-event slot-counter tripwire (chunk 3 smoke test
  already covers; cross-listed here).
- [x] Update `frontend_lib/CODE_OVERVIEW.md` with the slot
  machinery summary.

### Chunk 5 — Live integration test (stage 2 deliverable) ✅

Five live scenarios against the demo workstation Python
process. The existing `_demo_workstation.py` was sufficient
without modification — the `_Note` entity it already registers
gives us a real loadable target, and an unknown short_id is
trivial to construct on the client side.

- [x] **Happy load.** Allocate a load for `e-test`'s short_id,
  write the stub into state. `stub.result` transitions
  pending → filled with the Note's decoded payload (verified
  via `value.body === "frontend_lib integration test note"`).
  Stub's `committed` / `done` atoms also flip.
- [x] **Dedup.** Two `allocateLoad(ref)` calls for the same
  shortId return the same stub (`stubA === stubB`). The
  single load completes once.
- [x] **Preserve / unpreserve via session_debug.** After a
  fill arrives + a follow-up flush, the slot's seq appears
  in `session_debug.preserved_events`. Removing the slot
  drops the seq from `preserved_events` on the next commit.
- [x] **Fill survives across detach + reattach.** Set up
  preserved fill on session A, detach. A fresh
  `attachSession(sessionA.sessionId)` from a new connector
  receives the snapshot containing both the slot reference
  in committed state AND the fill in `preserved_events`; the
  new session's `stub.result` resolves to filled with the
  same payload. (Proves preserve+resubscribe end-to-end —
  the essence of reconnect behavior without needing to break
  a live socket.)
- [x] **Invalid short_id rejection.** Allocate against
  `"does-not-exist"`; the server's op-time validation rejects
  with `validation_failed` (the load worker never spawns, so
  there's no `LoadErrorEvent` — the original chunk-5 plan
  underspecified this path). The mutation rolls back with a
  `permanent` `RollbackInfo`; the stub's `result` stays
  `pending` (slot was never committed); the
  `validation_failed` canary fires.
- [x] Update `frontend_lib/CODE_OVERVIEW.md` and
  `projects/fxtr_workstation/CODE_OVERVIEW.md`.

5 new tests in `integration.test.ts` (637 tests green total).

## Design notes to consult

- `design_notes/03_stub_types.md` — `OpResultStub` shape and
  inner atoms.
- `design_notes/04_session_state_layout.md` — preserve-events
  flow, reducer responsibility table. **Note**: stage 2 supersedes
  the "auto-mutations from the reducer" guidance — we don't
  auto-flush in stage 2 (events update `slotFills` and bump
  `lastProcessedSeq` but don't trigger a mutation). Revisit when
  there's a real need to drain the event_log between user
  writes.
- `design_notes/05_dispatch_and_inline_loading.md` — slot dedup
  motivation.

## Discussion-topics updates

Add to `design_notes/discussion_topics.md`:

- **[x] Multi-mutator preserve-set drift.** Solved by
  re-deriving `committedPreservedSeqs` on every committed
  state change rather than tracking our emitted ops. Hinges on
  protocol compliance from foreign mutators; canary is the
  `validation_failed` reject reason on
  `PreserveEventsOp` / `UnpreserveEventsOp`; protocol-violating
  fallout surfaces via the broken-stub mechanism. → this file.
- **[x] Stub identity across rollback.** Stubs are stateless
  wrappers around `(slot, session)`; observationally
  indistinguishable across rollback + foreign-commit-at-same-
  slot. No `optimisticAllocations` eviction needed. → this
  file.
- **[x] Broken-stub handling.** New `["broken_op_result", slot]`
  wire tag + per-session `brokenSlots` set + atom override
  surface manually-constructed stubs, cross-session smuggling,
  and protocol-violating foreign-removal as observable
  errored fills. → this file.
- **[ ] Auto-flush for event ack/drain.** Deferred. Servers'
  `event_log` will grow between user mutations; revisit when
  this becomes a memory concern.

## Open questions

- **`OpResultStub<T>`'s type parameter is best-effort.** After
  a rollback where the underlying slot ends up resolving to a
  different op (only possible in multi-mutator sessions), a
  consumer who smuggled the stub out of our state tree could
  see data not matching the TS type. Acceptable: at runtime
  types are erased; consumers should validate via `$fxtr.type`
  dispatch. Docstring on the concrete class flags this.
- **Decode pipeline laziness.** Eager decode at fill-arrival
  time. Lazy decode is explicitly out of scope; reconsider if
  fill payloads grow large.
- **`slotFills` purge aggressiveness.** Only purges on
  committed state changes. Doesn't actively GC on
  subscribe-failure / connector-close (those are terminal
  anyway). Acceptable.
- **Strict broken-stub detection on `userState.set`.**
  Currently we detect at flush time, which means broken
  writes silently accumulate until the next flush. We could
  alternatively walk the new value at write time and throw —
  more eager, but pays a walk cost per write. Punting unless
  the deferred detection produces real user confusion.

## Post-stage-2 retrospective notes

These came up during the chunk-5 retrospective and are worth
revisiting before stage 3 (invokes, was 5) and stage 6 (callbacks)
inflate the surface further:

- **`session.ts` is ~1700 lines and growing.** Natural split
  before stage 3: `session_subscription.ts` (FSM + register),
  `session_reducer.ts` (flush + computeOps + flushWalk +
  reindexCommittedState), `session_events.ts` (applyEvent +
  fill handlers + snapshot rebuild). Functional today; this
  is purely a navigability concern.

- **`WorkstationOpResultStub`'s constructor is publicly
  exported.** Anyone can `new WorkstationOpResultStub(slot,
  session)` and write the result into state. The flush walk
  catches it (treats as broken / cross-session) but it'd be
  cleaner to lock construction to a symbol-keyed factory
  inside the session. Trivial to add when it becomes a real
  footgun.

- **Slot bookkeeping is fragmented across many maps with
  distinct lifecycles** (`opResultStubs` append-only,
  `pendingSlotSpecs` cleared end-of-flush + on reject,
  `brokenSlots` permanent, `slotFills` purged on reindex,
  `committedSlotShortIds` re-derived,
  `committedPreservedSeqs` / `committedSlotSet` re-derived,
  `committedAckSeq` mirror, `localSlotCounter` monotonic).
  Each is individually documented but reasoning about a
  slot's full state requires hopping. Worth reconsidering
  whether to consolidate into a single `Map<slot, SlotEntry>`
  discriminated union before invokes / callbacks add their
  own maps.

- **`loadDedup` carry-forward across foreign state_updates**
  (chunk-2 hack): superseded by
  `stage2_post1_wire_load_dedup.md` — `loadDedup` is now a
  wire-visible reserved key on the envelope. Foreign loads
  participate in dedup naturally; the persistent
  `loadOpSlotShortIds` map went away; `absorbState` reads
  `loadDedup` straight from the wire.
