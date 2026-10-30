# Stage 6 — Callback inboxes

Last piece of the workstation surface: the receive side of
workstation callbacks, modeled as inbox cursors in session state.
Adds the `["inbox", slot, offset]` wire form for state, the
`["callback", slot]` send-side handle as a first-class stub kind,
the per-slot in-memory event log, the create/close ops, and the
preserve-set extension for callback events.

The slot-lifecycle machinery built in stages 2-3 (allocator,
pendingSlotSpec map, flush walk, fill events, preserve-set
re-derive, broken-stub handling) is shaped for additive op kinds
and reused here mostly verbatim — stage 6 adds a `"callback"` arm
to `OpSpec`, a `kind: "callback"` branch to the `new-with-spec`
emission, a new `handleCallbackEvent`, and a new `inboxLogs` map
that the preserve-set derivation reads in addition to `slotFills`.

## Scope

In:

- Two stub kinds in `@fxtr-ui/core`: `CallbackHandleStub<R>` (send
  side, opaque), and the existing abstract `CallbackInboxStub<R>`
  extended with full public surface (`events`, `state`,
  `committed` atoms; `sliced(n)` cursor advance;
  `handle` getter producing the matching send-side stub).
- Type split in `@fxtr-ui/core`: rename `StructuredValue` →
  `StateValue`, add `ResultValue` (no client-only stubs) and
  `InvokeArgValue` (adds `CallbackHandleStub`, drops
  `OpResultStub`/`CallbackInboxStub`). Tightens what TS will let
  consumers write where.
- New `inbox` reserved tag in the wire codec; the existing
  `callback` tag becomes the wire form for `CallbackHandleStub`.
- Concrete `WorkstationCallbackInboxStub` and
  `WorkstationCallbackHandleStub`; both carry a session backref.
- `allocateCallback()` plumbed end-to-end: mints slot + inbox
  stub + `pendingSlotSpec`; flush emits `CreateCallbackOp`.
- `CloseCallbackOp` emission when every inbox stub for a slot
  disappears from local user state.
- Per-slot in-memory `inboxLogs[S] = { firstIndex, events[] }`
  populated from preserved `CallbackEvent`s. Trim-on-commit:
  `firstIndex` advances when committed-state `min(offset)`
  advances; entries drop entirely when no inbox stub remains.
- Preserve / unpreserve diff extended to include callback-event
  seqs filtered by local-state `min(offset)` per slot.
- Snapshot rebuild populates `inboxLogs` from preserved
  CallbackEvents.
- Cross-session-stub handling: a per-class dead-sentinel pattern
  (mirrors `OpResultStub`'s `slot=0` sentinel). Cross-session
  `CallbackInboxStub` in state gets substituted at flush time;
  cross-session `CallbackHandleStub` in invoke args throws at
  `allocateInvoke` time.
- Canonical invariants docstring on `WorkstationSession` covering
  loads, invokes, and the new inbox bookkeeping.

Out (deferred):

- Auto-mutation triggered on `CallbackEvent` arrival. Follow the
  same pattern as fills today: append to in-memory log, bump
  `lastProcessedSeq`, let the next user-driven flush carry the
  `PreserveEventsOp`. Revisit if we observe events being pruned
  before the user mutates.
- Server-driven "callback closed without our action" signal —
  no wire mechanism today; the `state` atom only has
  `opening | open | closed` from the local lifecycle.
- Optimizing `firstIndex` / `committedInboxSlotSet` into
  inflight envelope fields. Today we recompute on every committed
  change; same pattern as `committedSlotSet`. Optimization later
  if profiling justifies.

## Resolved design decisions

The conclusions of the design discussion before chunking. Each is
the *current* answer; we revisit if implementation surfaces new
constraints.

### Inbox-as-cursor identity

`CallbackInboxStub` is conceptually `(slot, offset)`. `offset` is
"events consumed by this cursor so far"; the slot's full delivered
history lives in `inboxLogs[slot].events` indexed absolutely. The
abstract base exposes `offset` (users manage it via `sliced`; they
may want to read it for relative-advance math or debug output) but
hides `slot` — slot is workstation-specific identity, not part of
the cursor semantics. Cursor advance goes through `sliced(n)`,
which produces a fresh stub instance (no interning).

### No built-in `cleared()`; "clear visible events" is
`inbox.sliced(events.length)`

An earlier draft had `inbox.cleared()` that captured end-of-log
at call time via an imperative `store.get`. Removed: it was the
only place in the codebase that did an imperative session-atom
read, and it bypassed jotai's dependency tracking (calling it
inside a derived atom's getter would silently miss reactivity).

Users now compose explicitly:

- **Snapshot at render time** (common): read events reactively,
  pass length to `sliced`:
  ```ts
  const events = useAtomValue(inbox.events);
  // in click handler:
  setUserState(prev => ({ ...prev, inbox: prev.inbox.sliced(events.length) }));
  ```
- **Snapshot at action time** (rarer): compose a custom jotai
  write atom that reads `inbox.events` at action-fire time and
  slices. Idiomatic jotai composition; no library magic.

Forces the choice of "when did I observe the events I'm
clearing?" to be explicit in user code.

### `firstIndex` is derived

We do NOT store a per-slot "discarded count" in the wire envelope.
Invariant: `inboxLogs[S].firstIndex == min(stub.offset for inbox
stubs in committed user state for S)`. On reconnect the snapshot's
`preserved_events` arrive containing exactly the events from
`min(offset)` onward (server preserves what we told it to), so the
index is recomputable from committed state alone. Mirrors how
`committedSlotSet` is derived rather than stored.

### Trim on commit, not on flush

`reindexCommittedState` (which already re-derives slot-related
indices on every committed change) extends to walk the new
committed user state, recompute `min(offset)` per inbox slot, and
trim `inboxLogs[S]` front-events accordingly.
`handleCallbackEvent` only appends; the flush only emits
preserve / unpreserve ops, never mutates `inboxLogs` directly.
Rollback is automatic — we never optimistically trim, so there's
nothing to recover.

### Send side as a separate stub kind

`CallbackHandleStub<R>` is a first-class stub type, exposed via
`inbox.handle`. The handle's wire form is `["callback", slot]`;
the inbox's wire form is `["inbox", slot, offset]`. This avoids
a context-aware encoder (same value, two different wire shapes
depending on where it appears), keeps the wire ↔ type
correspondence 1:1, and aligns with the Python side where the
send side really is a separate bridged service ref.

Tradeoff: the user writes `session.allocateInvoke(svc, "method",
[inbox.handle])` instead of `[inbox]`. Acceptable; the explicit
`.handle` makes the send/receive distinction visible.

### Type split: `StateValue` / `InvokeArgValue` / `ResultValue`

```ts
// payloads from Python services (load/invoke/callback results).
// no client-only stubs.
type ResultValue =
  | null | boolean | number | string | Uint8Array
  | EntityRefStub<unknown> | ServiceRefStub<unknown>
  | readonly ResultValue[]
  | { readonly [key: string]: ResultValue };

// what the user can put in userState. adds the receive-side stubs.
type StateValue =
  | ResultValue
  | OpResultStub<unknown> | CallbackInboxStub<unknown>
  | readonly StateValue[]
  | { readonly [key: string]: StateValue };

// what the user can pass as invoke args. adds the send-side handle.
type InvokeArgValue =
  | ResultValue
  | CallbackHandleStub<unknown>
  | readonly InvokeArgValue[]
  | { readonly [key: string]: InvokeArgValue };
```

`ResultValue ⊆ StateValue ∩ InvokeArgValue` structurally. Renames
existing `StructuredValue` callers to `StateValue` (sweep through
core, connector-workstation, tests). `Method<Args, Kwargs,
Returns>` generic constraints tighten to `InvokeArgValue` /
`ResultValue` per context.

`unescapeWire` becomes overloaded so the absence of
`lookupOpResult` / `lookupInbox` in opts narrows the return type
to `ResultValue`; their presence widens to `StateValue`.

### Cross-session smuggling

Different surfaces, different responses:

- **State** (`CallbackInboxStub`): the flush walk substitutes any
  stub whose `session !== this` with the session's per-class
  dead-sentinel — same pattern as `OpResultStub`'s `slot=0`
  sentinel. Wire form is `["broken_inbox", 0]`; the server stores
  wire state opaquely so this is a pure frontend convention
  (mirroring `["broken_op_result", 0]`).
- **Invoke args** (`CallbackHandleStub`):
  `assertInvokeArgValueAllowed` (extended) throws synchronously
  at `allocateInvoke` time with a path-pointed error. Caller is
  in a sync context and can react.

Why different: state-writes accumulate atomically — substituting
a bad leaf preserves the rest of the mutation. Invoke args are
imperative; throw lets the caller handle it directly.

Note that `EntityRefStub` / `ServiceRefStub` need no
cross-session check — they're connector-scoped (not
session-scoped); identity is just the shortId.

### Don't intern inbox/handle stubs

Each `sliced(n)` mints a fresh `CallbackInboxStub`.
`inbox.handle` returns a fresh `CallbackHandleStub` on each call
(or cached lazily on the inbox instance — either is fine).
`leafEqual` in `state.ts` learns to compare inbox stubs by
`(slot, offset)` and handles by `slot`. Referential equality
isn't a useful invariant for cursor values.

### Mostly-opaque abstract surfaces

`CallbackHandleStub` is fully opaque — no public getters, just
the brand + `debugSummary`. `CallbackInboxStub` exposes `offset`
(cursor position is part of the abstraction; users manage it via
`sliced` and may want to read it) but hides `slot` (workstation-
specific identity). Wire encode-side accesses the slot via
`encodeInbox` / `encodeCallbackHandle` opt callbacks on
`EscapeOptions` (mirrors how `encodeOpResult` works today).

## Canonical invariants — add to `WorkstationSession` class doc

Lifted into the class JSDoc so all slot-related invariants live
in one place. Pulls in the existing load/invoke ones; introduces
the new inbox ones.

> ### Slot bookkeeping invariants
>
> **`slotFills` retention.** For every slot `s` in `slotFills`:
> - `slotFills[s].seq <= committedAckSeq` ⇒ `slotFills[s].seq ∈
>   committedPreservedSeqs` (the server is preserving it).
> - `slotFills[s].seq > committedAckSeq` ⇒ event still in the
>   server's unacked log; no preserve op needed yet.
>
> `reindexCommittedState` purges entries that fall below the ack
> horizon without being preserved.
>
> **`loadDedup` authority.** `local.loadDedup` and
> `committed.loadDedup` are wire-visible (per
> `stage2_post1_wire_load_dedup.md`). `committedSlotShortIds` is
> the inverse index of `committed.loadDedup` and is rebuilt by
> `reindexCommittedState`.
>
> **Inbox event log derivation.** For every slot `s` in
> `inboxLogs`:
> - `inboxLogs[s].firstIndex == min(stub.offset for inbox stubs
>   in committed user state for s)`.
> - `inboxLogs[s].events` corresponds 1:1 (in seq order) with the
>   `CallbackEvent`s the server is preserving for slot `s` —
>   i.e. seqs in `committedPreservedSeqs` whose originating event
>   was a `CallbackEvent(slot=s)`.
>
> `reindexCommittedState` advances `firstIndex` and drops
> front-events when committed `min(offset)` advances; drops the
> entire `inboxLogs[s]` entry when no inbox stub for `s` remains.
>
> **Slot monotonicity.** `localSlotCounter ≥` every slot we've
> ever seen (locally allocated, observed on the wire, or in a
> snapshot's `slot_counter`). `lookupOpResult` and
> `handleFillEvent` throw on `slot > localSlotCounter` — ordering
> bug.

## Implementation chunks

Each chunk lands as a reviewable PR. Review-order rationale: type
split is a sweeping but mechanical rename that's much easier to
review alone; codec + stub scaffolding next; allocate + flush
wires the create/close protocol; event reception + preserve flow
completes the runtime; tests at the end against a real
workstation server.

Each chunk's detailed plan lives in
`stage6_chunk_plans/stage6_chunkN_plan.md`, written when that
chunk begins.

### Chunk 1 — types, codec, stubs, allocate scaffolding

- Type split in `@fxtr-ui/core`: rename `StructuredValue` →
  `StateValue`; add `ResultValue`, `InvokeArgValue`; retighten
  `Method<>`, `Session.userState`, `FxtrSessionUpdateCtx`.
  Sweep callers across the workstation connector + tests.
- Abstract surface: `CallbackInboxStub` grows public `events`,
  `state`, `committed`, `handle`, `sliced(n)`. New abstract
  `CallbackHandleStub`. Both opaque (no slot/offset getters on
  the base).
- Wire codec: new `TAG_INBOX = "inbox"`; encode/decode for
  `["inbox", slot, offset]`; new `encodeInbox` /
  `encodeCallbackHandle` / `lookupInbox` opts. The `["callback",
  slot]` decode path is removed entirely — handles are
  client→server only, the server never sends this tag, so the
  decoder always throws on encountering it. `unescapeWire`
  overloaded so absence of `lookupOpResult`/`lookupInbox`
  narrows return to `ResultValue`.
- Concrete `WorkstationCallbackInboxStub`,
  `WorkstationCallbackHandleStub`, per-class dead-sentinels.
- Session-level placeholder atoms (initialized empty; consumed
  in chunks 2-3): `inboxLogsAtom`, `committedInboxSlotSetAtom`.
- `allocateCallback`: mints slot + inbox stub + `pendingSlotSpec`
  `{kind: "callback"}`. The spec sits unconsumed until chunk 2
  wires the flush walk.
- Canonical invariants docstring added to `WorkstationSession`
  (load + invoke + the inbox section shipping in final form even
  though the runtime hasn't caught up yet).
- Tests: codec unit tests for `inbox` and retyped `callback`
  tags; a type-level smoke test that `StateValue` rejects
  `CallbackHandleStub` and `InvokeArgValue` rejects
  `CallbackInboxStub` / `OpResultStub`.

After chunk 1, callbacks compile end-to-end. No
`CreateCallback` / `CloseCallback` ops are emitted; no events
are delivered. If chunk 1 starts feeling oversized in review,
we can break out the type-rename sweep as chunk 1a.

### Chunk 2 — flush walk + create/close emission

- `OpSpec` grows `{kind: "callback"}` arm (no payload — create op
  only needs the slot).
- `flushWalk`: traverses `CallbackInboxStub` leaves, collects
  per-slot `min(offset)`, substitutes cross-session inboxes with
  the per-session dead-sentinel.
- `computeOps`: emits `CreateCallbackOp` for `new-with-spec`
  callback slots, `CloseCallbackOp` for slots present in
  committed but absent in local.
- `state.ts` extensions: `walkCollect` surfaces inbox slots +
  per-slot `min(offset)`; `leafEqual` compares inbox stubs by
  `(slot, offset)` and handles by `slot`; `isPlainContainer`
  recognizes `CallbackHandleStub`.
- Tests: unit tests for the flush walk + computeOps against
  synthetic committed-vs-local pairs (broken stubs, cursor
  advance, close-on-absent). Integration tests still deferred
  to chunk 4.

After chunk 2, the lifecycle protocol works (create + close fire
correctly) but events aren't delivered to the consumer.

### Chunk 3 — event reception + preserve flow + snapshot rebuild

- `applyEvent` routes `kind: "callback"` to
  `handleCallbackEvent`, which appends to
  `inboxLogs[slot].events` (creating the entry on first event
  with `firstIndex = 0`).
- `computeOps` preserve calc: for each inbox slot in local user
  state, preserve `events[i].seq` for
  `i ≥ (local_min_offset - firstIndex)` filtered by
  `seq <= ackSeq`. Unpreserve seqs no longer in the desired
  set. Runs alongside the existing `slotFills` preserve loop.
- `rebuildSlotFillsFromSnapshot` (or a sibling
  `rebuildInboxLogsFromSnapshot`): for each preserved
  `CallbackEvent`, append to `inboxLogs[slot]` in seq order;
  `firstIndex` per slot = `min(offset)` in the absorbed
  committed state.
- `reindexCommittedState`: maintains
  `committedInboxSlotSetAtom`; trims `inboxLogs[S]` when the new
  committed `min(offset)` advances; drops the entire entry when
  no inbox stub for `s` remains in committed.

After chunk 3, callbacks fully work end-to-end. The canonical
invariants from the class docstring hold.

### Chunk 4 — integration tests + remaining unit tests

End-to-end test against the live Python workstation server,
extending `tests/_integration_harness.ts`:

- Allocate a callback inbox.
- Pass `inbox.handle` as an arg to an invoke targeting a Python
  service that calls `.notify()` a few times.
- Confirm the inbox receives the notifications (via `events`).
- Exercise `sliced(n)` cursor advancement; verify preserve /
  unpreserve traffic.
- Remove every inbox stub from state; verify `CloseCallbackOp`
  is emitted and the Python service-side `_closed` flips.
- Simulate a connection drop + reconnect mid-stream; verify
  preserved CallbackEvents are redelivered and `inboxLogs`
  reaches the same state.

Plus any unit tests on `state.ts` / `flushWalk` /
`reindexCommittedState` that didn't ship with chunks 2-3.

## Design notes to consult

- `design_notes/03_stub_types.md` — `CallbackInboxStub` (and the
  new `CallbackHandleStub`); inbox-as-cursor design; the
  "send-side ref" tangent (now resolved by the explicit handle
  stub).
- `design_notes/04_session_state_layout.md` — per-slot event log
  management; min-cursor preservation logic.

## Implementation steps (stage-level)

1. [x] Chunk 1 — types, codec, stubs, allocate scaffolding.
   See `stage6_chunk_plans/stage6_chunk1_plan.md`.
2. [x] Chunk 2 — flush walk + create/close emission. See
   `stage6_chunk_plans/stage6_chunk2_plan.md`.
3. [x] Chunk 3 — event reception + preserve flow + snapshot
   rebuild. See `stage6_chunk_plans/stage6_chunk3_plan.md`.
4. [x] Chunk 4 — integration tests + remaining unit tests. See
   `stage6_chunk_plans/stage6_chunk4_plan.md`.
5. [x] Update CODE_OVERVIEW.md with the final inbox machinery
   description. Done via per-chunk updates plus a consolidated
   pass after chunk 4 (callback methods + integration scenarios
   + plan/design link); rewritten to the
   [`code_overview_guide`](../../../../../../v2/notes/code_overview_guide.md)
   style (no historical chunk/stage framing, just current state).
