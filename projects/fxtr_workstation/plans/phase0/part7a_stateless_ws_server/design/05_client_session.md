# 05 — Client session (the reducer fork)

Status: resolved. The session implements the abstract `Session`
surface over purely local state, emulating what a workstation
session would do with the browser as the workstation.

## Re-orientation pointers

The abstract surface to satisfy:

| What | Where |
|---|---|
| `Session` interface (`sessionId`, `store`, `lifecycle`, `userState`, `detach`) | `fxtr-ui-core/src/session.ts:287-363` |
| `FxtrSessionUpdateCtx` (the five `allocate*`) | `fxtr-ui-core/src/session.ts:152-260` |
| `FxtrSessionAtom` / `FxtrUpdate` / `UserStateWriteOptions` / `RollbackInfo` | `fxtr-ui-core/src/session.ts:104-280` |
| Abstract stubs (`OpResultStub` atoms, `CallbackInboxStub` surface) | `fxtr-ui-core/src/stubs.ts:106-260` |
| Value families (`ResultValue`/`StateValue`/`InvokeArgValue`) | `fxtr-ui-core/src/types.ts:45-94` |

The workstation implementation being forked
(`fxtr-ui-connector-workstation/src/session.ts`, 3026 lines):

| Piece | Lines | Fate |
|---|---|---|
| Constructor, atoms, ctx wiring | 832-919 | **keep** (slimmed) |
| `allocateLoad`/`allocateInvoke`/`allocateCallback`/metadata allocs + `allocateDeduped` | 1055-1337 | **keep** (op firing changes) |
| Flush scheduling + `flushWalk` | 1432-1448, 2435-2597 | **keep**, repurposed (below) |
| `computePatch` (JSON patch), `computeOps` (wire ops) | 1569-1767 | **drop** (no wire mutations) |
| Inflight envelope, `handleCommit`/`handleReject`/`rollbackInflight` | 1771-1904 | **drop** (commits are local + immediate) |
| `applySnapshot`/`applyStateUpdateEvent`/`absorbState`, seq reconciliation | 1957-2015, 2376-2394, 2733-2781 | **drop**; replaced by storage rehydrate |
| `applyEvent`/`handleFillEvent`/`handleCallbackEvent` | 2017-2095, 2167-2201 | **keep** as the local fill/notify entry points |
| `reindexCommittedState` | 2628-2725 | **keep**, simplified (GC role, below) |
| Preserve/unpreserve derivation | (within flush/reindex) | **drop** (no retention horizon) |
| `detach`/`terminate` | 1016-1040, 1357-1380 | **keep**, local semantics |
| Stub classes | `stubs.ts:29-369` (incl. `WorkstationStubSession` backref interface `:115-154`, `FillRecord` `:63-65`, `InboxLog` `:74-97`) | **fork** near-verbatim |
| State walks | `state.ts:85-92` (`collectStubsInState`); `diffStateToPatch :169-177` | **keep walk**; **drop diff** |
| Escape codec | `escape.ts` | **keep** as persistence codec; resolved mode added (01) |

## The write path

```
store.set(session.userState, updater, opts?)
  → microtask-coalesced flush (as today, session.ts:1432-1448)
  → run updaters with ctx; allocate* validate + record specs
      (throws here = the ONLY failure mode; nothing remote can
       reject a mutation)
  → flushWalk: substitute cross-session stubs (kept), classify
      slots, collect stubs
  → commit: local atom becomes the new committed state, atomically
  → schedule debounced persist (storage write)
  → fire newly-allocated ops asynchronously:
      load     → arena fetch (03)        → fill / errored fill
      invoke   → http fetch or ws invoke (01) → fill / errored fill
      metadata → local resolve (03)      → fill (sync-ish)
      callback → mint inbox (below)      → committed immediately
```

There is no inflight envelope, no ack, no reject path. `onRollback`
(`core/session.ts:127-137`) is retained for API compatibility but
essentially never fires — remote failures arrive as **errored
fills**, exactly like a workstation `invoke_error`. `terminate()`
resolves locally: mark closed, delete the session's storage keys,
registry entry removed. `detach()` flushes pending persists and
releases the instance. The `assertWritable` gating (writes during
terminate/detach reject) is kept as-is.

Local seq counter: fills and inbox entries still carry `seq`s
(stub atoms and `InboxLog` ordering depend on them); a per-session
monotonic counter stamped at delivery replaces the server's event
seq. No ack horizon exists, so nothing is ever pruned by seq — slots
and logs are GC'd structurally instead (below).

## Slots, fills, GC

Slot machinery is kept: monotonic `localSlotCounter`, op-result and
inbox stub interning, slot-0 dead sentinels, cross-session
substitution, load/metadata dedup via `allocateDeduped`
(`session.ts:1288-1337`), invokes never deduped.

`reindexCommittedState`'s fork has one job: after each commit, walk
committed state (`collectStubsInState`) and drop `slotFills` /
`inboxLogs` entries whose slots no longer appear — the client-side
analogue of retention, otherwise discarded slots accumulate in
storage forever. (The workstation version's other duties — preserve
seqs, dedup-info reverse index for patch emission — disappear; the
dedup *forward* maps stay, they back `allocateDeduped`.)

## Persistence + rehydration

Per session, one storage value (04 key schema) holding:

```
{version, metadata, createdAt, slotCounter, seqCounter,
 userState: <shortId-mode escape>,            // escape.ts as-is
 dedup: {load: {...}, get_entity_metadata: {...}, ...},
 slotFills: {slot: FillRecord-with-escaped-value | "pending-load"},
 inboxes: {slot: {uuid, log: escaped InboxLog}}}
```

Written debounced after each commit and on fill/notify delivery.
Rehydrate (= `attachSession`): decode `userState` via `unescapeWire`
state-mode with connector lookups; load fills marked pending and
refired through the arena; **invoke fills persist as-is; invokes
pending at save time rehydrate errored ("interrupted")**
(open_questions #9, answered); inbox logs restore verbatim;
transient-service stubs restore broken (01). Unknown shortId during
decode (table corruption) → that slot/leaf becomes the broken
sentinel + loud console.error, session still loads.

## Inboxes and `subscribe_to`

`allocateCallback()` mints slot + a persisted inbox UUID. The inbox
is committed immediately (state `"open"` on next read — there's no
server round trip; `stubs.ts:262-348`'s `"opening"` phase collapses).
Notify frames arriving on any pool connection route by UUID →
`handleCallbackEvent` fork appends to `inboxLogs[slot]` (decode =
result-mode unescape of resolved-mode payloads). `sliced(n)` /
offsets work unchanged.

Invokes targeting `inbox.handle` (the workstation's
callback-target invoke, `session.ts:1086-1189` overload) dispatch
**locally** by method:

- `subscribe_to(source, method="subscribe")` → desugars to
  `allocateInvoke(source, method, [handle])` under the hood — one
  ordinary invoke; its fill is whatever the source returned (ws:
  transient unsubscribe service; http: anything/null, inert). The
  `method` param mirrors the precursor's `subscribe_to(method=)`
  generalization (part6 stage4post1 Chunk 4; resolved N2), defaulting
  to `"subscribe"`. No replay on reconnect (overview.md governing
  principle; recovery is the Part 1 dispatcher change reacting to the
  `connectionState → broken` signal below).
- other methods (resolved Q-F) → implement `notify(payload)` locally
  (append to own log; useful for tests), error on anything else.
  Workstation inboxes only meaningfully expose
  `subscribe_to` / `notify` / `listening`.

**Connection-state synthesis (resolved N1).** The abstract
`CallbackInboxStub` carries `connectionState` (`idle` / `connected` /
`broken`) and `subscriptionBreakReason` since part6 stage4post1. On
the workstation these are driven by server `CallbackSubscriptionEvent`s;
here the inbox is client-local, so the **connector synthesizes them**:
a successful `subscribe_to` over a ws source flips the inbox to
`connected` (recording the (url, generation) it bound to); when that
connection's generation dies (design/01 §Disconnect), the connector
flips the inbox to `broken` with a reason. http-source subscriptions
never reach `connected` (no channel) and stay `idle`. This is the
uniform breakage signal the Part 1 dispatcher reload reacts to.

Closing (removing the last inbox stub from state) drops the routing
entry; the session sends a **best-effort `unsubscribe`** invoke on a
still-live transient handle (resolved Q-E — mirrors the workstation
parker's teardown, `callback_impl.py`). This requires the session to
remember handle↔inbox associations (the (inbox slot → transient
unsubscribe handle) recorded when `subscribe_to` filled).

## Lifecycle

`connecting` only during rehydrate (sync-ish), then `connected`;
never `stale` (open_questions #12); `closed` on terminate or
discovered-terminated (registry entry gone — e.g. another tab
terminated it; detected lazily on storage read, **not** via storage
events for now). ws connectivity never affects session lifecycle.
