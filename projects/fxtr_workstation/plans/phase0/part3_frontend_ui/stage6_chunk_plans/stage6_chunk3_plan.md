# Stage 6, Chunk 3 — CallbackEvent reception + preserve flow + snapshot rebuild

Closes the loop on chunk 6 by wiring the receive side. After chunk 2,
`CreateCallbackOp` / `CloseCallbackOp` fire correctly but no
`CallbackEvent`s are delivered to the consumer — the per-slot
`inboxLogs` atoms stay empty. Chunk 3 hooks the event stream, the
snapshot rebuild path, and the preserve / unpreserve diff so the
`inbox.events` atom emits real payloads, the server preserves the
right events past `ack_seq`, and the canonical "Inbox event log
derivation" invariant from `WorkstationSession`'s class doc holds.

Read [`../stage6_callback_inboxes.md`](../stage6_callback_inboxes.md)
for design decisions and [`stage6_chunk2_plan.md`](./stage6_chunk2_plan.md)
for the scaffolding chunk 3 builds on.

## Scope

In:

- **`handleCallbackEvent(event)`** new method on `WorkstationSession`:
  - Slot-counter tripwire (mirrors `handleFillEvent`).
  - Decode `event.payload` via `unescapeWire` in result-mode
    (no `lookupOpResult` / `lookupInbox` — callback payloads are
    `ResultValue`).
  - Append `{seq, payload}` to `inboxLogs[event.slot].events`,
    creating the log with `firstIndex = 0` on first event for a
    slot.
  - Decode failures synthesize no entry and log; future events
    for the slot still flow normally.
- **Event routing** in `applyEvent`: new `kind: "callback"` arm
  routing to `handleCallbackEvent`. Existing fill-kinds path
  unchanged. `bumpProcessedSeq` runs unconditionally afterward
  (same shape as today).
- **`rebuildInboxLogsFromSnapshot(frame)`** new method:
  - Walks the absorbed committed state for inbox slot
    min-offsets.
  - For each callback event in `frame.preserved_events ∪
    frame.subsequent_events`, decodes payload (result-mode),
    appends to `inboxLogs[slot]`.
  - On first event for a slot, sets `firstIndex` to that slot's
    committed min-offset (the server preserves events from
    `min_offset` onward, so the first preserved event's
    absolute index equals `min_offset`).
  - Sorts each slot's events by seq to ensure order.
  - Wired into `applySnapshot` after `absorbState`, before
    `reindexCommittedState`.
- **`reindexCommittedState` extensions**:
  - Compute new `committedInboxSlotOffsets` from
    `stubs.inboxSlotOffsets` (already collected by the chunk-2
    walk).
  - **Trim-on-commit**: walk old `inboxLogs`:
    - Slot not in new committed map → drop the entry entirely.
    - Slot in committed with `new_min_offset > old.firstIndex`
      → slice off `new_min_offset - old.firstIndex` front
      events, bump `firstIndex` to `new_min_offset`.
    - Slot in committed with no advance → keep the entry as is.
  - **`committedPreservedSeqs` extension**: include inbox event
    seqs from the trimmed logs where `seq <= committedAckSeq`,
    alongside the existing `slotFills` contribution.
  - (Slot-fills purge logic unchanged — operates on the
    unified preserve set.)
- **`computeOps` preserve diff extension**: for each slot in
  `walked.inboxSlotsInLocal`, walk
  `inboxLogs[slot].events[i ≥ local_min_offset -
  log.firstIndex]` and add seqs `<= ackSeq` to
  `desiredPreservedSeqs`. The existing preserve / unpreserve
  diff against `committedPreservedSeqs` handles emission.
- **Tests** in `tests/callback_reducer.test.ts`:
  - `handleCallbackEvent`: appends to log, decodes payload,
    creates log on first event, malformed payload logs but
    doesn't crash, slot-counter tripwire.
  - `inbox.events` atom: emits payloads after CallbackEvent
    arrival.
  - `computeOps` preserve flow: `PreserveEventsOp` fires for
    callback seqs <= ack_seq when the cursor is at offset 0;
    `UnpreserveEventsOp` fires for seqs consumed past by
    `sliced`.
  - `reindexCommittedState` trim: cursor advance trims front
    events and bumps `firstIndex`.
  - `reindexCommittedState` drop: removing the only inbox stub
    drops the `inboxLogs` entry entirely.
  - Snapshot rebuild: preserved CallbackEvents populate
    `inboxLogs` with `firstIndex = min(offset)` in absorbed
    committed state; subsequent_events extend it.

Out (deferred):

- Integration tests against live workstation (full lifecycle
  with real CreateCallback / notify / Close). → Chunk 4.
- Auto-mutation on event arrival (eagerly send
  `PreserveEventsOp` per fill). Today fills + callbacks both
  wait for the next user-driven mutation to ship preserve ops;
  see the stage-6 design doc's deferred-out items.

## File touches

- `frontend_lib/packages/fxtr-ui-connector-workstation/src/session.ts`:
  add `handleCallbackEvent`, route in `applyEvent`, add
  `rebuildInboxLogsFromSnapshot`, wire into `applySnapshot`,
  extend `reindexCommittedState` + `computeOps`.
- `frontend_lib/packages/fxtr-ui-connector-workstation/tests/callback_reducer.test.ts`:
  new describes for event reception, preserve flow, snapshot
  rebuild, and reindex trim/drop.

## Implementation steps

1. [x] Add `handleCallbackEvent` to `WorkstationSession`. Route
   `kind: "callback"` from `applyEvent` to it.
2. [x] Extend `computeOps` preserve calc to include inbox event
   seqs filtered by per-slot `local_min_offset` and `seq <=
   ackSeq`. Both fills and callbacks feed `desiredPreservedSeqs`.
3. [x] Extend `reindexCommittedState` with the inbox trim
   (front-trim on `min_offset` advance, entry-drop on slot
   removal) and the inbox contribution to
   `newPreservedSeqs`.
4. [x] Add `rebuildInboxLogsFromSnapshot`. Wire into
   `applySnapshot` AFTER `absorbState`, BEFORE `reindexCommittedState`.
5. [x] Tests in `tests/callback_reducer.test.ts` for the new
   behaviors. 8 new tests covering event reception, decode
   pass-through for entity/service refs, malformed-payload
   skip, preserve/unpreserve flow, trim-on-commit, drop on
   removal, snapshot rebuild with advanced cursor.
6. [x] `pnpm check`, `pnpm test`, `pnpm lint` from `v2/ts`;
   all green (804 tests pass, up from 796).
7. [x] Update `CODE_OVERVIEW.md` with the new event-reception
   and trim-on-commit behavior.

## What chunk 4 does with this

- Spins up a real `_demo_workstation.py` server.
- Allocates a callback, passes the handle to an invoke
  targeting a Python service that calls `.notify()` several
  times.
- Asserts the inbox's `events` atom emits the notifications in
  order, the wire-level `PreserveEventsOp` carries the right
  seqs, `sliced` advances commit a `UnpreserveEventsOp`,
  removing the stub fires `CloseCallbackOp`, and a
  reconnect-mid-stream replays preserved events into the new
  session's `inboxLogs`.
