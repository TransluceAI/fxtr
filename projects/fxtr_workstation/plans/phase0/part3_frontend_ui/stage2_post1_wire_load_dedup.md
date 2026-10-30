# Stage 2 post-cleanup 1 — Wire `loadDedup` for cross-mutator dedup

A targeted refactor that lifts `loadDedup` from a local-only cache
into a wire-visible reserved key on the session state. Closes two
gaps left at the end of stage 2:

1. **Foreign-mutator loads aren't deduppable today.** In a
   multi-mutator session, a foreign client's load slot has no
   `slot → shortId` mapping anywhere on our wire — we'd need
   either a `session_debug` subscription (cached + racy) or a
   server protocol extension to learn it. So `allocateLoad(X)`
   redundantly mints a fresh slot even when foreign already has
   one for X.
2. **Attaching to an existing session doesn't bootstrap dedup.**
   `attachSession(sessionId)` arrives with a snapshot whose
   `user` state has load slots, but our local
   `loadOpSlotShortIds` map is empty (we didn't allocate them).
   The first `allocateLoad(X)` on the attached session also
   mints a fresh slot.

The fix: make the `shortId → slot` mapping part of committed
state, so every subscriber reads the same authoritative dedup
index from the wire.

## Design

The session envelope's wire-visible shape extends from `{user}`
to `{user, loadDedup}`:

```jsonc
state: {
  user: { ... user-controlled state ... },
  loadDedup: {
    "<shortId>": <slot:number>,
    ...
  }
}
```

The reserved-key extensibility was already anticipated in
`design_notes/04_session_state_layout.md` ("additional reserved
keys may be added later").

### Map shape

Single slot per shortId, not a list. Protocol-level
serialization guarantees there's at most one in-flight load per
(shortId, mutator) — if two mutators race, the server's stale-ack
rule rejects the loser, who rolls back and re-reads the winning
mutator's `loadDedup[X]=winner-slot` on the rebase. So at any
committed point in time there's at most one slot for `X`.

Empty entries: delete the key entirely on the last slot's removal
(no `{"X": null}` or `{}` representations).

### Encoding

`loadDedup`'s value is a flat `{string: number}` dict. No stubs,
no bytes, no literal lists. The existing wire-escape codec walks
it as a plain object — no special-case handling needed in the
encoder or decoder.

The `state` field on the wire continues to be a single
JSON-encoded dict; JSON Patch ops just gain `path` prefixes like
`/loadDedup/<shortId>` alongside the existing `/user/...` paths.

## Changes

### `LocalEnvelope` and `wirePart`

```
LocalEnvelope {
  user?: UserState
  terminated?: true
  loadDedup: ReadonlyMap<string, number>   // unchanged from stage 2
}
```

The `loadDedup` field stays where it is in the local envelope —
but its role flips from "local-only mirror" to "client-side
representation of the wire-visible `state.loadDedup` field."
`wirePart` extends to project `loadDedup` into the wire-state
output (skipping the key when empty, to avoid pointless empty
dicts on the wire for fresh sessions):

```
wirePart(env) ⇒ {
  ...(env.user !== undefined ? {user: env.user} : {}),
  ...(env.loadDedup.size > 0 ? {loadDedup: Object.fromEntries(env.loadDedup)} : {}),
}
```

The existing `diffStateToPatch` then naturally produces patch
ops over both `/user/...` and `/loadDedup/...` paths.

### Flush walk

The walk's existing `walk.loadDedup` output (a reconciled
`Map<shortId, slot>` derived from local user state) becomes the
**source of truth for the wire write**. Today it's used to
update `local.loadDedup` for the next allocate; that role stays,
but now the same map also drives the JSON Patch via `wirePart`.

One detail to handle: the walk needs to know each slot's shortId
to build `loadDedup`. Currently it falls back to
`loadOpSlotShortIds` for slots that are already in committed
(no pendingSlotSpec). After this change, `loadOpSlotShortIds` is
gone — the equivalent info lives in `committed.loadDedup`
(inverse direction: shortId → slot). The walk has two choices:

- **Scan `committed.loadDedup` to find the shortId for a given
  slot.** O(distinct loaded entities) per slot lookup. Typical
  UI sessions have ≤ tens of loads — fine.
- **Maintain a session-level `committedSlotShortIds: Map<slot,
  shortId>` rebuilt in `reindexCommittedState`.** O(1) lookups
  at the cost of a small extra rebuild. Cheap.

Default to the session-level inverse for cleanliness; the cost
is negligible.

### `allocateLoad`

Reads `local.loadDedup[X]` (unchanged from today). The hit can
now point at a slot we never minted (foreign's). For those, we
mint the corresponding `WorkstationOpResultStub` lazily via the
existing slot-interning map (`opResultStubs`) the same way wire
decode does today. Returns the stub; the rest of `allocateLoad`
(spec recording, dirty bit) stays as-is for fresh mints.

If the dedup hit points at a slot whose fill we haven't received
yet, the stub's `result` atom reads `slotFills[slot] === undefined`
and reports `pending`. Once foreign's fill arrives (it lands in
our `slotFills` via the regular `LoadResultEvent` path on our
subscription), `result` transitions to `filled` and the consumer
sees the data. Identical timing to today's "we allocated this
slot ourselves but the fill hasn't arrived yet" path.

### `absorbState`

The chunk-3-era rebuild via `computeLoadDedup` goes away.
`absorbState` now just decodes the new committed state from the
wire (which includes `loadDedup`), and the local envelope
inherits it directly. The rebuild logic shifts from "walk the
new user state and look up shortIds locally" to "trust the wire"
— simpler and correct by construction.

### Things that get deleted

- `WorkstationSession.loadOpSlotShortIds` (persistent
  `slot → shortId` for our own loads — wire `loadDedup` now
  carries this for *all* loads, including foreign's).
- `WorkstationSession.computeLoadDedup` (used by `absorbState`
  to rebuild from `loadOpSlotShortIds` — `absorbState` just
  reads the wire now).
- The "incremental write to local.loadDedup on allocateLoad"
  branch in `WorkstationSession.allocateLoad` — the dedup
  entry only lands in the envelope at flush time, alongside
  the wire write. Between `allocateLoad` and the flush, dedup
  hits against the new shortId go through `pendingSlotSpecs`
  instead (the spec carries the shortId, so we can answer
  "did we just allocate X?" by scanning `pendingSlotSpecs`).

### Things that get added

- `WorkstationSession.committedSlotShortIds: Map<slot, shortId>`
  rebuilt by `reindexCommittedState` from
  `committed.loadDedup`. Used by the flush walk to look up
  shortIds for slots that are in committed user state.

### Same-tick allocate dedup

Today: `allocateLoad(X)` writes through to `local.loadDedup[X]`
synchronously, so a second `allocateLoad(X)` in the same tick
hits the cache.

After this change: `local.loadDedup` doesn't get the new entry
until the flush walk runs (microtask). For the same-tick second
allocate to dedup correctly, we need a same-tick lookup path
that consults `pendingSlotSpecs` (slot → spec, including
shortId). Concretely:

```
allocateLoad(X):
  1. Scan pendingSlotSpecs for an entry whose spec is
     {kind: "load", shortId: X}. If found, return the stub.
  2. Read local.loadDedup[X]. If hit, look up the stub.
  3. Else mint fresh + record pendingSlotSpec.
```

Step 1 is the same-tick dedup. Step 2 is the post-commit /
post-rebase dedup. Step 3 is the cold path.

Scanning `pendingSlotSpecs` is O(n) where n is "allocations
done in this tick". Negligible in practice.

## Multi-mutator race trace

Two mutators A and B attached to the same session both call
`allocateLoad(X)` simultaneously.

1. A: walk finds no `committed.loadDedup[X]`, no spec for X →
   mint slot 5. Spec[5] = {kind: load, X}.
2. B (same wall-clock moment): same → mint slot 6. Spec[6] =
   {kind: load, X}.
3. A flushes: patch adds `["op_result", 5]` to user state +
   `loadDedup.X = 5`. `LoadEntityOp(5, X)`. `ack_seq = K`.
4. B flushes: patch adds `["op_result", 6]` + `loadDedup.X = 6`.
   `LoadEntityOp(6, X)`. Same `ack_seq = K`.
5. Server serializes. Suppose A wins:
   - A's mutation commits at `seq = K+1`. `committed.loadDedup.X
     = 5`.
   - B's mutation is rejected as `conflict`
     (`ack_seq < session.last_state_seq` from a different
     mutator).
6. B rolls back. Local resets to committed (which now has
   `loadDedup.X = 5`). `pendingSlotSpecs` cleared. Slot 6 is
   dead.
7. User on B calls `allocateLoad(X)` again (via the rebase or
   their `onRollback` retry). Step 2's `local.loadDedup[X]`
   hits with slot 5. Returns A's stub. No second wire load.

At every committed point along the way, `loadDedup[X]` has at
most one slot — exactly because the protocol's stale-ack rule
serializes concurrent writers. Hence the single-slot shape.

## Subtle: slot stubs minted by dedup hits

When `allocateLoad(X)` dedups against a slot that we didn't
mint ourselves (foreign, OR our own from an earlier session
instance attached via `attachSession`), the stub is minted
lazily via the slot-interning map (`opResultStubs`). The stub
carries `(slot, session=this)` and reads `slotFills` for its
result. Identical to the lazy mint that happens on wire-decode
of `["op_result", slot]` inside an incoming state_update.

This means the cross-session-stub substitution from chunk 3
still works correctly: stubs minted by *us* (via dedup hits or
via decode) have `session === this`; stubs minted by another
session's reducer have a different `session`. The flush walk's
cross-session detection sees the difference and substitutes
with the dead-sentinel.

## Tests to add / update

- Existing chunk-3 dedup tests should still pass with minimal
  change (the test for "two allocateLoad(X) before commit
  dedup" exercises step 1 of the new allocate path; the test
  for "dedup persists across optimistic write + remove" still
  works since loadDedup is in the envelope and rolls back).
- New test: **foreign-load dedup.** Session A attaches and
  loads X. Session B (also attached to the same session_id)
  calls `allocateLoad(X)` after A's mutation commits. B gets
  A's stub (same slot integer); B's flush emits no
  `LoadEntityOp`. Verifiable via `session_debug.open_loads`
  showing exactly one entry for the duration of the load.
- New test: **attach-session dedup bootstrap.** Session A
  loads X, commits, detaches. Session B attaches; snapshot
  brings `loadDedup.X = <A's slot>`. B calls `allocateLoad(X)`
  and gets a stub at the same slot, no new load emitted.
- New test: **multi-mutator race.** A and B both
  `allocateLoad(X)` concurrently; one wins, one rolls back +
  rebases; both end up with the same slot.

## Implementation order

This is a single cohesive change; no need to split into
sub-chunks. Rough order:

1. Extend the wire shape: `wirePart` projects `loadDedup`;
   `decodedAsEnvelope` reads it back. The codec needs no
   changes (it's already a plain dict).
2. Refactor `WorkstationSession`:
   - Delete `loadOpSlotShortIds`, `computeLoadDedup`.
   - Add `committedSlotShortIds`; populate in
     `reindexCommittedState`.
   - Update the flush walk to consult `committedSlotShortIds`
     for slots already in committed (replaces the
     `loadOpSlotShortIds` lookup).
   - Drop the incremental `local.loadDedup` write in
     `allocateLoad`; add a `pendingSlotSpecs` scan for
     same-tick dedup.
3. Update the existing chunk-3 / chunk-4 tests as needed (the
   semantics are the same; some test scaffolding that pokes
   at `loadDedup` may need adjustment).
4. Add the three new tests above.
5. Update `frontend_lib/CODE_OVERVIEW.md` and the post-stage-2
   retrospective section of `stage2_entity_loading.md` (mark
   the foreign-dedup gap as closed; drop the
   `loadOpSlotShortIds` mention).

Estimated scope: a few hundred lines of churn in `session.ts`,
mostly deletions; ~4 new tests; one wire-shape change. About
half a day.
