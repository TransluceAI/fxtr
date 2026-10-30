# 04 — Session state layout

This pins down *what's actually in the wire `state` JSON* and how
the local reducer / atom layer reflect it.

Builds on `01_reactive_model.md` (the high-level model),
`02_connector_abstraction.md` (the session interface), and
`03_stub_types.md` (the four stub types).

## Wire-level layout

The wire `state` dict has a **fixed top-level schema** managed by
the session reducer. The user never writes to it directly — they
write to the user envelope through the Session API (via jotai atoms),
which hides this layout. The user has no key-naming restrictions
inside their own state.

```jsonc
{
  // User-controlled state. Free-form; arbitrary keys / nesting;
  // stubs can appear anywhere.
  "user": { ... },

  // (additional reserved keys may be added later; for now `user`
  // is the only top-level key.)
}
```

**No `stub_results` map.** Fills (load / invoke results) are not
copied into wire state. Instead, the client tells the server to
**preserve** result events for any slot referenced from state.
Preserved events stay alive past `ack_seq`, are re-delivered on
reconnect, and have their inner entity / service refs pinned by
the server. See the next section.

### Preserved events as the storage for fills

The key workstation primitive:
**clients can ask the server to preserve session events by sequence
number.** Preserved events stay alive on the server, keep their own
referenced data alive, and are re-delivered on reconnect.

This means:

- The slot fill stays in the workstation's event log; the
  client's local view derives `slotFills` from preserved
  `LoadResultEvent` / `InvokeResultEvent` / `LoadErrorEvent` /
  `InvokeErrorEvent`.
- Entities and services referenced *inside* a preserved event
  are pinned by the server (it already does this for unacked
  events; preservation extends the lifetime).
- On reconnect, `subscribe_session` snapshot includes preserved
  events alongside committed state; the client reconstructs its
  local view without surgery.

### What gets preserved

Preservation is **inferred from state**, like entity / service
pinning. The reducer's state walk collects:

- entity short_ids → `TrackEntity` set
- service short_ids → `TrackService` set
- **slot ints referenced by `OpResultStub` instances in state →
  preserve events referencing this slot**
- **slot ints referenced by `CallbackInboxStub` instances in
  state → preserve events referencing this slot (with cursor offset, see below)**

Diff against committed pin / preserve sets at mutation time;
emit the appropriate ops alongside user patches and ops.

### Inbox preservation and offsets

For inbox stubs `["inbox", slot, offset]`: each cursor implicitly
keeps the slot's event log alive *from `offset` onward*. The
server should retain S's events from `min(offsets across all
inbox stubs for S in state)` to the latest.

When all inbox stubs for a slot move past an event, the server
can drop it. When all inbox stubs are removed from state, the
slot is unpreserved entirely and the callback receive-end can
be closed (matches the `CloseCallbackOp` semantics from earlier).

## Local atom layout

The session's source-of-truth atom holds something like:

```ts
type SessionAtomValue = {
  // server-authoritative wire state, structured form (stubs as
  // instances at leaves)
  committed: StructuredState,

  // local view of preserved slot results, derived from
  // LoadResult/InvokeResult/error events for slots in the
  // preserve set
  slotFills: Map<slot, FillValue>,

  // per-slot consumed inbox event lists, derived from preserved
  // CallbackEvents (truncated below the min cursor offset)
  inboxLogs: Map<slot, InboxEntry[]>,

  // session-level dedup map: short_id of a loaded entity → the
  // slot that loaded it. Used to dedup repeat load allocations
  // (see slot-dedup design below).
  loadedEntities: Map<short_id, slot>,

  // optimistic pending patch waiting for ack
  pendingPatch: StructuredState | null,
  pendingMutation: MutationInFlight | null,

  // counters
  committedSeq: number,
  committedAckSeq: number,
  committedSlotCounter: number,
  localSlotCounter: number,    // ≥ committed; advances on allocateX
};
```

Stubs at leaves are class instances, with inner atoms that read
from these maps (see `03_stub_types.md`). Components don't see
the distinction; they read a slot atom and get the value.

## Slot dedup for repeat loads

When the user calls `session.allocateLoad(refstub)`, the session
checks `loadedEntities`:

- If `refstub.short_id` is in the map, return an `OpResultStub`
  reusing that slot. Two consumers writing the "same" load slot
  into different state locations don't kick off a second wire
  load.
- Otherwise allocate a new slot. The new stub is returned. When
  the user stores it into state, the reducer's mutation:
  1. emits `LoadEntityOp(slot, short_id)`,
  2. configures us to preserve the slot event when it arrives,
  3. adds `short_id` to `loadedEntities` on commit.

When the load event arrives, the session must detect that we need to preserve
events for this slot, and emit/enqueue the relevant preserve op to preserve it.
It can then cache the result locally, and rebuild it from the re-delivery when
we reconnect.

When the last reference to a slot is removed from state, the
reducer's mutation unpreserves the relevant events. On unpreserve, the entry
is removed from `loadedEntities`; a future allocate for the same
short_id will get a fresh slot.

Dedup is **always-on** for entity loads. Entities are immutable and
content-addressed, so this is always safe.

## The session reducer's responsibilities

Per arriving event:

| Event kind         | Reducer action                                         |
| ------------------ | ------------------------------------------------------ |
| `state_update`     | replace `committed`; bump seq counters; recompute      |
|                    | `localSlotCounter = max(committed_slot, local)`        |
| `load_result`      | update `slotFills[slot]` with decoded value. Send or   |
|                    | queue the event for preservation on the server, if     |
|                    | still alive.                                           |
| `load_error`       | same, but with an `errored(message)` value             |
| `invoke_result`    | same as load_result                                    |
| `invoke_error`     | same as load_error                                     |
| `callback`         | append to `inboxLogs[slot]`. Send or queue event to    |
|                    | preserve until min cursor passes.                      |

Per local mutation request (user dispatching a state write):

1. Compute structured-form diff of (committed + new pending) vs.
   (committed + old pending).
2. From the diff, derive:
   - JSON patch operations against the wire state form.
   - Implicit ops:
     - `TrackEntityOp` / `UntrackEntityOp`
     - `TrackServiceOp` / `UntrackServiceOp`
     - `LoadEntityOp` / `InvokeOp` / `CreateCallbackOp`
     - `CloseCallbackOp`
     - `PreserveEventsOp` / `UnpreserveEventsOp`
3. Bundle `MutateSessionRequest(ack_seq, update_id, patch, ops)`.
4. Wait for ack/reject; on success commit; on reject, run
   `onRollback` callback and drop the pending patch.

Note that we will need to send a mutation to the server to inform it that we need it to
save events that deliver results to our slots, via `PreserveEventsOp`. This can either
be sent eagerly (as soon as we receive the fill) or enqueued to ride along with the
next state mutation. Op result data is _not_ writable via the public slot API, so we
do not need to actually mutate our own user-facing state when informing about events
to preserve; we just need to ensure the server is told that we still need those events
so it doesn't discard them when we ack the event delivery (via our next outgoing state
update).

## Resolved

- **OQ-α — wire layout:** Strict envelope. For now just `user` at
  the top level. Reducer-managed regions (originally
  `stub_results`) eliminated by the preserve-events primitive.
- **OQ-γ — slot counter on rollback:** No reset on rollback, but
  the local counter is always advanced past the maximum of
  whatever appears on the wire (foreign updates can introduce
  higher slot ints, e.g. when a foreign client commits a
  conflicting mutation; we absorb that and resume above it). Rule:
  `localSlotCounter = max(localSlotCounter, observed_wire_max) + 1`
  on every `allocateX`.
- **OQ-δ — inbox offset:** Count of events consumed for this slot.
- **OQ-ε — pin walking:** State walk collects entity/service refs
  (for pin diffs) and slot ints (for preserve diffs). No special
  case for fills since they live in preserved events, not state.

## Auto-mutation queueing

Auto-mutations (sending `PreserveEventsOp` for newly-received fills
for open slots) flow through
the same outgoing mutation system as user-initiated mutations,
batched so that the `ack_seq` of the outgoing mutation matches the
sequence number of the last event we have fully processed, and
serialized one in-flight at a time.

The simplest approach is to immediately send a state update to the
server acknowledging each event we receive in the read queue,
with a `PreserveEventsOp` and an empty mutation, where the read queue
is intentionally serialized with component-driven state updates. This
means that we immediately "ack" every event we receive as soon as we
receive it and apply its effects to our state.

Pros:

- Matches the wire's own per-session mutation queue, so we never
  have two mutations concurrently in flight that the server would
  serialize anyway.
- Simpler ack accounting: the response always corresponds to the
  one mutation we just sent.
- The same mutator (us) always sees its own mutations as
  acceptable bases, so even foreign updates that interleave only
  matter at the boundary between our mutations.

Cons (acceptable for now):

- An idle stream of auto-mutations could starve out a user write.
  Mitigation: we could allow user writes to let their mutations
  "jump ahead" of events we have received from the server, as long
  as those user writes are _ack-seq-consistent_: the state that the
  component has when the component-driven set-atom-state function runs
  must be the state _before_ the non-acknowledged server-sent-events
  are processed or applied to the state, so that we specify a sequentially
  consistent ordering of state updates.
