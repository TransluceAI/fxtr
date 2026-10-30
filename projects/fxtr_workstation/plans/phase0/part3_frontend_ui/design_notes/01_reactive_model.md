# 01 — The reactive state model

This is the foundational design note. Everything else (connectors,
hooks, dispatch, panel rewrite) hangs off the decisions here.

## Decisions locked in

### A. One source-of-truth atom per session, with `splitAtom`-style projections.

- The session is the unit of reactive state. Each connected session
  exposes a single canonical atom that owns its full local view.
- Components subscribe via projections (analogue of jotai's
  `splitAtom` / `selectAtom`) so each component re-renders only when
  the slice it cares about changes.
- The projections must be writeable in the obvious way (write to a
  child = write to the parent), because mutations must round-trip as
  JSON-patch updates against the wire `state`. We will probably need
  to roll our own variant of `splitAtom` since the stock helpers
  don't quite handle "write a record key" / "push to end of array"
  efficiently while preserving patch derivability.

### B. Async ops are driven entirely by the source-of-truth atom — no side channels.

- Imperatively *creating* a load / invoke / callback request returns a
  small **stub** object (`OpResultStub<T>` or `CallbackInboxStub<R>`)
  that holds a freshly-allocated slot integer. **No wire traffic is
  sent at this point.**
- The act of *writing the stub into the atom* (i.e. into the session
  state tree) is what triggers the wire mutation. The connector's
  reducer diffs the new state against the last committed state,
  detects the new pending stub, and bundles the appropriate
  protocol op (`LoadEntityOp` / `InvokeOp` / `CreateCallbackOp`) into
  the same `MutateSessionRequest` that carries the JSON patch.
- The result event flows back, the reducer fills the slot, and any
  component that subscribes to a projection touching that stub's
  position re-renders with the resolved value.
- This makes the system fully reactive end-to-end: a fresh client
  connecting to the same session can see the pending slot, the in-
  flight op, and the eventual fill, all from state + the event log
  alone.

### C. Slot tokens are first-class values in the session state.

- Inside the atom, the state tree is a structured JS tree: plain
  primitives + arrays + objects + **stub instances** at leaves.
- Stub instances include:
  - `EntityRefStub<T>` — a workstation-tracked entity (short_id +
    optional type tag).
  - `ServiceRefStub<T>` — a workstation-tracked service.
  - `OpResultStub<T>` — a pending or filled slot. Three states:
    `pending`, `filled(value)`, `errored(message)`.
  - `CallbackInboxStub<R>` — a pending or open slot for a created
    callback. The "fill" is conceptually a stream of payloads, not a
    single value (see open question OQ-2 below).
- On the wire, the state is serialized via a list-escape codec that
  encodes each stub as a tagged form. Decoding reconstructs the
  stubs. Other connected clients reconstruct the same structured
  state from the wire form.
- The slot-fill events (`LoadResultEvent`, `InvokeResultEvent`,
  `CallbackEvent`, and their error variants) feed a sparse
  `slot_fills` map maintained alongside the structured state. The
  reducer projects fills into stubs at read time; from the
  component's perspective, an `OpResultStub<T>` is reactive over its
  `pending` → `filled` transition.

### D. Trackers (entity / service pins) are derived implicitly from state.

- The session reducer walks the structured state, collects every
  `EntityRefStub` and `ServiceRefStub` short_id at rest, and
  diffs that against the last committed pin set. The diff yields
  `TrackEntityOp` / `UntrackEntityOp` / `TrackServiceOp` /
  `UntrackServiceOp` ops that get bundled into the next mutation.
- Stubs that only appear inside a *result event* but haven't been
  written into state are auto-pinned by the workstation server via
  the unacked-event retention horizon. Frontend code that wants to
  keep them past the next ack must move them into state — this is
  the explicit, observable rule.
- This walking happens against the **structured** state (not the
  serialized wire form). The codec / schema layer doesn't need to
  participate.

### E. Reducer/dispatch is the only commit path.

- Components never poke the websocket directly. They write to the
  atom, and the reducer turns commits into wire mutations.
- The reducer is responsible for:
  1. Maintaining the structured state, slot_fills, slot counter,
     committed-slot set, and pending-pin set.
  2. Computing the JSON patch between committed and pending state
     (post-projection: the patch operates on the wire JSON form).
  3. Inferring side-effect ops (load / invoke / create_callback,
     plus track / untrack ops) from the diff.
  4. Sending one `MutateSessionRequest` per "transaction" (see
     OQ-3 below).
  5. Handling acks / rejections / foreign updates / fill events.

## Concrete shapes (sketch)

```
SessionAtomState {
  // Server-authoritative state, last committed
  committedState: StructuredTree,
  committedAckSeq: number,
  committedSeq: number,

  // Local pending edits — applied optimistically in the projection,
  // not yet acked
  pendingPatch: Patch | null,
  pendingMutationId: number | null,

  // Slot accounting
  committedSlotCounter: number,
  // local in-flight allocations not yet committed
  reservedSlots: Set<number>,

  // Sparse slot fill map; populated from result events
  slotFills: Map<number, FillRecord>,

  // The set of trackers the server thinks we've pinned
  committedEntityPins: Set<short_id>,
  committedServicePins: Set<short_id>,

  // Recent unacked events (for resync after disconnects)
  unackedEvents: SessionEvent[],
}

OpResultStub<T> {
  slot: number,
  // structural metadata for the reducer to know what op to emit
  // when this is first written into state
  spec: { kind: "load", short_id } | { kind: "invoke", ... },
  // resolved value lookup is reactive against the parent atom's
  // slotFills map; component-facing read returns
  // { state: "pending" | "filled" | "errored", value? }
}
```

The "projected view" that components actually see merges
`committedState`, `pendingPatch`, and `slotFills`. The atom's read
side is this projection; the atom's write side schedules a
mutation.

## Resolved follow-ups

- **Stub wire shape (was OQ-1).** Lean simple: `["op_result", slot]`.
  No automatic opspec summary on the wire — if user code wants
  debug context, it stores it adjacent to the slot in state. The
  workstation already has a debug stream
  (`subscribe_session_debug`) for in-flight info.
- **Callback inbox semantics (was OQ-2).** Append-only history;
  inbox stubs are read cursors with `["inbox", slot, offset]`. See
  `03_stub_types.md` and `04_session_state_layout.md`.
- **Filled-vs-pending in wire state.** Stubs stay in their original
  position in the user-controlled state tree forever (until the
  user explicitly removes them). The session maintains a top-level
  `stub_results` map (still inside session state, on the wire);
  fills land there as auto-mutations from the reducer. The atom
  layer projects so consumers can read a slot atom and see the
  resolved value transparently. See `04_session_state_layout.md`.
- **Conflict / rollback (was OQ-4).** Default behavior: drop the
  optimistic pending patch. Expose an optional `onRollback`
  callback on each write so the caller can broadcast / surface a
  toast / kick off a retry. No automatic rebase; rollback gives
  enough hooks for someone who wants rebase to layer it on.

## Deferred to discussion

- **Mutation transaction boundaries (OQ-3).** Maybe we don't need
  explicit transactions; might be fine to treat each atom write as
  its own small mutation and let the user keep invariants.
  Composes with jotai differently depending on the choice. Tracked
  in `discussion_topics.md`.
- **Schema system (OQ-5).** Stripped-down bicodec cousin vs. zod
  vs. plain TS interfaces with no runtime validation. Big topic;
  tracked in `discussion_topics.md`.

## Still open

### OQ-6 — Per-projection patch derivation

For the `splitAtom`-like projections to round-trip into JSON
patches, the projection has to know its path. Two implementation
shapes:
- Build paths into the projection at construction (`atomAtPath(atom,
  ["foo", "bar", 3])` → child atom whose writes know the path).
- Treat projections as opaque atom-pairs (read + write callbacks)
  and require the user to pass paths separately.

The first is much nicer to use. We need to figure out the type
plumbing — probably something like a `.field("foo").index(3)` chain
that builds the path at the type level.

### OQ-7 — How does the atom handle the "structured form"?

Internal state holds stub *instances* at leaves. Two ways to keep
this representation:
- (a) The atom holds the structured form directly. We never serialize
  except when sending mutations / reading from the wire.
- (b) The atom holds a normalized JSON form (with stubs flattened to
  their tagged shape) plus a side index of stub instances. Faster
  diffing, harder to use.

(a) is far simpler and better for ergonomics. Diffing across
mutations can use referential equality of stubs (since they're
allocated once and immutable). Defaulting to (a) unless we hit a
real perf wall.
