# Stage 6, Chunk 2 — Flush walk + create/close emission

Wires the lifecycle protocol for callback slots. After chunk 1 the
session minted an inbox stub when `allocateCallback()` was called
and recorded a `{kind: "callback"}` `pendingSlotSpec`, but the
flush walk didn't see inbox leaves and `computeOps` didn't emit
any callback-related wire ops. Chunk 2 closes that loop.

After this lands: writing an inbox stub into `userState` fires
`CreateCallbackOp` on the next flush; removing every inbox stub
for a slot fires `CloseCallbackOp`. `CallbackEvent` reception
and preserve / unpreserve still wait for chunk 3, so the receive
side is wired but no events are delivered to consumers yet.

Read [`../stage6_callback_inboxes.md`](../stage6_callback_inboxes.md)
for the locked design decisions and
[`stage6_chunk1_plan.md`](./stage6_chunk1_plan.md) for the
scaffolding chunk 2 builds on.

## Scope

In:

- `state.ts` extensions:
  - `CollectedStubs` grows `inboxSlotOffsets: ReadonlyMap<slot,
    minOffset>` — per-slot minimum of inbox-stub offsets across
    the walked tree.
  - `walkCollect` populates it from `WorkstationCallbackInboxStub`
    leaves (excluding the `slot=0` dead-sentinel).
  - `leafEqual` compares `WorkstationCallbackInboxStub` instances
    by `(slot, offset)` — different offsets diff as different.
    `CallbackHandleStub` doesn't appear in `StateValue`-shaped
    trees (it's invoke-arg-only), but we add the type-guard
    for completeness in `isPlainContainer`.
- Session reducer extensions:
  - `FlushWalkResult` grows
    `inboxSlotsInLocal: ReadonlyMap<slot, minOffset>`.
  - `flushWalk` traverses `WorkstationCallbackInboxStub` leaves:
    - records the min-offset per slot;
    - classifies each slot the same way as op-result slots:
      `new-with-spec` (slot has `{kind: "callback"}`
      `pendingSlotSpec`) or `legitimate` (slot present in
      `committedInboxSlotSet`); the spec-vs-committed split
      drives whether `CreateCallbackOp` fires.
    - `slot === 0` (the inbox dead-sentinel from the chunk-1
      class) passes through with no bookkeeping.
  - `computeOps`:
    - Adds a `spec.kind === "callback"` arm to the
      `new-with-spec` slot loop → emits
      `CreateCallbackOp(slot)` (no payload).
    - After the existing track / preserve diffs, adds a loop over
      `committedInboxSlotSet - inboxSlotsInLocal` → emits
      `CloseCallbackOp(slot)` for each slot removed from local
      user state.
  - `reindexCommittedState` populates
    `committedInboxSlotSetAtom` from committed-state inbox
    leaves (via the extended `walkCollect`). Mirrors the
    existing `committedSlotSetAtom` derivation.
- Tests:
  - Extend `state.test.ts` to cover the new `inboxSlotOffsets`
    collection and `leafEqual`-by-`(slot, offset)` behavior.
  - New `callback_reducer.test.ts` (or extend `slot_reducer.test.ts`,
    decide at implementation time) for the
    `CreateCallbackOp` / `CloseCallbackOp` emission paths,
    new-with-spec dispatch, multi-stub min-offset, and
    legitimate-no-emit (inbox stub in committed state, no spec).

Out (deferred):

- `CallbackEvent` reception, `inboxLogs` population, preserve /
  unpreserve diff for callback events, snapshot rebuild for
  inbox logs. → Chunk 3.
- Integration tests against live workstation. → Chunk 4.

Cross-session inbox-stub substitution at flush time is **in
scope** for chunk 2 (added during a mid-chunk follow-up). New
wire tag `["broken_inbox", 0]` mirrors `["broken_op_result", 0]`;
the server stores wire state opaquely, so the tag is purely a
frontend convention.

## File touches

- `frontend_lib/packages/fxtr-ui-connector-workstation/src/state.ts`:
  extend `CollectedStubs`, `walkCollect`, `leafEqual`,
  `isPlainContainer`.
- `frontend_lib/packages/fxtr-ui-connector-workstation/src/session.ts`:
  extend `FlushWalkResult`, `flushWalk`, `computeOps`,
  `reindexCommittedState`.
- `frontend_lib/packages/fxtr-ui-connector-workstation/tests/state.test.ts`:
  new tests for `inboxSlotOffsets` + `leafEqual` for inbox.
- `frontend_lib/packages/fxtr-ui-connector-workstation/tests/callback_reducer.test.ts`
  (new): tests for `CreateCallbackOp` / `CloseCallbackOp`
  emission.

## Implementation steps

1. [x] Extend `CollectedStubs` in `state.ts` with
   `inboxSlotOffsets`. Update `walkCollect`, `leafEqual`
   (compares inbox by `(slot, offset)`), and
   `isPlainContainer` (recognizes `CallbackHandleStub`).
2. [x] Extend `FlushWalkResult` with
   `inboxSlotsInLocal: ReadonlyMap<slot, minOffset>`. Update
   `flushWalk` to traverse `WorkstationCallbackInboxStub`
   leaves: record per-slot min-offset and classify
   new-with-spec for callback specs into the shared
   `newWithSpecSlots` set.
3. [x] Add `spec.kind === "callback"` arm to `computeOps` →
   emit `CreateCallbackOp(slot)`.
4. [x] Add close-callback diff in `computeOps`: for each
   slot in `committedInboxSlotSet` not in
   `inboxSlotsInLocal`, emit `CloseCallbackOp(slot)`.
5. [x] Update `reindexCommittedState` to populate
   `committedInboxSlotSetAtom` from `inboxSlotOffsets.keys()`.
6. [x] Update `state.test.ts` to cover the new
   `inboxSlotOffsets` collection (4 cases) and `leafEqual`-by-
   `(slot, offset)` for inbox stubs (5 cases) + add/remove
   patches.
7. [x] Add `tests/callback_reducer.test.ts` (9 cases):
   allocate + write → create; allocate-and-discard → no
   create; two allocates → two distinct slots; cursor-advance-
   on-committed → no create/close (just patch); remove-only-
   stub → close; multi-stub min-offset → no close on partial
   removal; allocate-and-close-same-tick → no-op; reallocate-
   after-close → fresh slot; allocateCallback type sanity.
8. [x] `pnpm check`, `pnpm test`, `pnpm lint` from `v2/ts`;
   all green (790 tests pass, up from 772).
9. [x] Update `CODE_OVERVIEW.md` with the new flush-walk and
   `computeOps` behavior; cross-reference chunk 3 for event
   reception.

## What chunk 3 does with this

- Routes `CallbackEvent` to `handleCallbackEvent`; appends to
  `inboxLogs[slot]`.
- Extends `computeOps`'s preserve-event calc to include
  callback-event seqs filtered by per-slot
  `(inboxSlotsInLocal[slot] - inboxLogs[slot].firstIndex)`.
- Extends `rebuildSlotFillsFromSnapshot` (or sibling) to seed
  `inboxLogs` from preserved CallbackEvents.
- Extends `reindexCommittedState` to trim `inboxLogs` when the
  committed min-offset advances.
