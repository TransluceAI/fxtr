# Discussion topics tracker

A running list of design questions that came up in note-writing
sessions and need their own focused discussion later. Each entry has
a status and a link to where it's resolved (once it is).

Symbols: `[ ]` open · `[~]` partially explored · `[x]` resolved (link
to the doc where it landed) · `[-]` deferred / not blocking phase 0.

---

## Cross-cutting

- [x] **Connector vs. session relationship.** One uniform Session
  interface; HTTP is a degenerate session that raises on
  service/callback ops. → `02_connector_abstraction.md`
- [x] **Reactive primitive choice.** Jotai, with custom extensions
  for path-aware projections. → `01_reactive_model.md`
- [x] **Mutation rollback policy.** Drop optimistic patch on
  rejection; expose an optional `onRollback` per write so the caller
  can react. No automatic rebase. → `01_reactive_model.md` §H

## Session / state model

- [x] **Slot wire shape.** Just `["op_result", slot]`; user code
  attaches its own debug data adjacent in state if it wants.
  → `03_stub_types.md`
- [x] **Where do filled results live?** Top-level `stub_results` map
  inside session state, managed by the session, surfaced through
  abstracted slot-result atoms so consumers can pretend to read the
  slot directly. → `04_session_state_layout.md`
- [x] **Inbox semantics.** Append-only history per slot in shared
  storage; inbox stubs `["inbox", slot, offset]` are independent
  read cursors. `inbox.sliced(n)` / `inbox.cleared()` produce new
  stubs the user writes back into state. → `03_stub_types.md`,
  `04_session_state_layout.md`
- [x] **Closing a callback.** Just remove the inbox stub from state;
  diff produces `CloseCallbackOp`. → `03_stub_types.md`
- [x] **Auto-mutations from the session reducer.** Eager (one
  auto-mutation per fill) for first cut; serialize through the
  same outgoing queue as user mutations; possibly add a hybrid
  debounce later. → `04_session_state_layout.md`
- [x] **Slot counter divergence.** No reset on rollback; always
  advance past `max(local, observed_wire_max)`.
  → `04_session_state_layout.md`
- [x] **Inbox offset units.** Count of events consumed for the
  slot. → `04_session_state_layout.md`
- [x] **Wire layout shape.** Strict envelope: top-level `user`
  only (for now). Reducer-managed `stub_results` map eliminated
  by the preserve-events primitive. User never sees the raw form.
  → `04_session_state_layout.md`
- [x] **Stubs as session-bound objects with inner atoms.** Each
  stub class exposes inner atoms (`result`, `committed`, `done`
  for OpResult; `events`, `state` for Inbox; etc.) derived from
  the session's source-of-truth atom. Interned per session.
  → `03_stub_types.md`
- [x] **Fill storage via preserve-events.** Backend supports
  marking events as preserved by stub index. We drop
  `stub_results` from state entirely; fills stay as preserved
  events server-side; client derives local `slotFills` /
  `inboxLogs` maps from preserved events. Re-delivery on
  reconnect falls out of the snapshot. → `04_session_state_layout.md`
- [x] **Slot dedup for repeat loads.** Session keeps a
  `loadedEntities` map; `allocateLoad` returns the existing
  `OpResultStub` if the slot is still preserved. Removes the
  need for EntityBox-style shared cache. → `04_session_state_layout.md`,
  `05_dispatch_and_inline_loading.md`
- [-] **Load-outside-session / load-for-side-effects.** Deferred
  to a later refactor. The plan: client-local entity cache that
  avoids re-fetching even across sessions. Out of scope for
  phase 0.

## Schema / type system

- [x] **No schema system for phase 0.** Runtime is "JSON tree
  with stub class instances at leaves," identified via
  `instanceof`. TS annotations + a tiny `entityType()` factory
  carrying just a type id. → `06_no_schema_for_now.md`. Layer
  bicodec-cousin / zod / etc. later if needed.

## Mutation API ergonomics

- [ ] **Transaction primitive.** Do we need explicit
  `session.transaction(...)` brackets, or is "every set is its own
  small mutation, user keeps invariants" good enough? Composes with
  jotai differently in each case (a transaction wraps multiple atom
  writes; without it, we coalesce by microtask). Captured as `OQ-3`
  in `01_reactive_model.md`.
- [ ] **Microtask coalescing.** If we *don't* go with explicit
  transactions, how do we batch multiple writes inside one event
  handler into one wire mutation?

## Dispatch + React layer

- [x] **EntityBox shape punted.** Replaced by simpler
  `(refstub, ownStorage)` pattern + session-level slot dedup.
  → `05_dispatch_and_inline_loading.md`
- [x] **Where does the load slot live for inline `<EntityLink>`?**
  In the component's `ownStorage` slice, allocated by the
  component itself. Session dedup handles the "two components
  want the same entity" case. → `05_dispatch_and_inline_loading.md`
- [ ] **Suspense vs. result-object.** With slots being explicit, do
  we still want Suspense as the integration story for the
  declarative wrapper layer, or always result-objects?
- [ ] **Dynamic dispatch registry.** How a runtime-extensible
  registry should look. Day-one we can use a constant; long-term we
  need per-session registry merging for agent-registered types.

## fxtr-workstation-core rewrite

- [ ] **Migration strategy.** Burn down + rewrite, or stage
  packages? The current core is fairly fxtr-react-shaped; some of
  the breadcrumb / sidebar / dockview wiring is independent and
  can move over.
- [ ] **Per-panel session model.** What goes in session state vs.
  panel-local React state vs. connector global state.

---

When a topic gets a focused discussion, the resolution lands in a
new note (or section of an existing one), and the entry above flips
to `[x]` with the link.
