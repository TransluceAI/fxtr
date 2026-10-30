# Stage 8 — Session-side caching primitives

Goal: enable smart clients to avoid round-tripping data they already have. Today, if a client receives a result event whose payload they want to keep using past the ack horizon, the only option is to mutate the data into session state — wasteful (data is already in the event log) and visible to all subscribers. Stage 8 adds three orthogonal primitives that decouple "what's tracked in the session" from "what's carried on the wire".

See [`./overview.md`](./overview.md) for the part-1 design and [`./stage3_sessions.md`](./stage3_sessions.md) for the session pipeline this stage extends.

## Scope

- **(a) Preserved events.** Per-session set of seqs that survive past `last_state_ack_seq`. Their `event_refs[seq]` refcount contributions stay alive; the events themselves stay in `event_log` and are re-delivered on resubscribe. New mutation ops `PreserveEventsOp(seqs: list[int])` / `UnpreserveEventsOp(seqs: list[int])`.
- **(b) Outside-session direct entity load.** New top-level `/client` request `LoadEntityRequest(short_id)` that returns the same escaped CBOR a `LoadEntityOp` would, with no session linkage and no refcount side effects. Children FxtrIDs in the payload get a short_id allocated (inactive — the records are not minted), so the descriptors are stable for display, but they are not lookupable for further direct loads unless something else makes them active.
- **(c) Silent load.** New mutation op `LoadEntitySilentOp(slot, short_id)` with the same refcount + workstation-tracking side effects as `LoadEntityOp`. The result event omits the payload (`LoadResultSilentEvent(seq, slot)`); failures still land as `LoadErrorEvent` to keep the error surface uniform.

The combination of (b) + (c) gives smart clients a cache strategy: hit (b) once for the bytes, invoke (c) any number of times to reproduce side effects.

## Non-goals (for stage 8)

- No new persistence semantics — preserved events are still in-memory only, evaporate with the server (consistent with the rest of phase 0).
- No batching of direct loads. One `LoadEntityRequest` = one entity. Pipelining is the client's job.
- No client-side library — the debug UI gets minimal visualization (8d), but the design assumes downstream clients (agent MCP, panel viewer) will use these primitives directly.

## Resolved design decisions

These were settled in the design discussion before drafting the plan; recorded here so the spec is self-contained.

- **Preserve scope.** Preserve only applies to result/notification events (load / invoke / metadata / callback). `state_update` events are *never* preservable — preserving them would muddy the "the most recent state_update is `last_state`" mental model. Op validation rejects PreserveEventsOp targeting a `StateUpdateEvent`.
- **Preserve eligibility.** A seq is preservable in mutation M iff (i) the event is currently in `event_log` (so `seq > old last_state_ack_seq`), (ii) `seq <= new ack_seq` (i.e. M's ack would otherwise drop it in the retention prune), (iii) seq is not already in `preserved_events`, and (iv) the event is not a `StateUpdateEvent`. Events still in the unacked-suffix (`seq > new ack_seq`) are not preservable — they are already preserved by virtue of being unacked.
- **Unpreserve eligibility.** A seq must currently be in `preserved_events`. Within-mutation `[Preserve(seq), Unpreserve(seq)]` is allowed and nets to no-op (mirrors tracker-op semantics).
- **Batched ops.** PreserveEventsOp / UnpreserveEventsOp take a `list[int]` for batching. Per-element validation; any failure rejects the whole mutation. (Tracker ops historically take a single short_id each, but they predate this convention; the batching shape here is forward-looking.)
- **Storage split: `event_log` stays contiguous; preserved events live in a sidecar.** `event_log` continues to hold only the unacked suffix (`seq > last_state_ack_seq`), preserving today's contiguous-prefix retention prune and its monotonicity assertion. A new `preserved_events: dict[int, SessionEvent]` sidecar on `SessionRecord` holds events that have been moved past the horizon by a `PreserveEventsOp`. Wire shape on `SessionSnapshotEvent.events` is unchanged: the snapshot builder concatenates `preserved_events.values()` (sorted by seq) with `event_log` and ships a single ordered list. Clients distinguish preserved (`seq <= last_state.ack_seq`) vs. unacked suffix (`seq > last_state.ack_seq`) by comparison.
- **Debug snapshot.** `SessionDebugSnapshotEvent` gains `preserved_events: list[int]` (sorted) so the debug UI can render them distinctly without reasoning about seq comparisons.
- **Retention prune stays a contiguous-prefix drop on `event_log`.** Because preserved events are moved out of `event_log` into the sidecar at preserve time, the prune itself never has to "skip over" anything — it walks the front of `event_log` and drops everything with `seq <= ack_seq`, exactly as today. The four cases collapse cleanly:
  - newly-acked, not preserved → moves through `event_log` retention as usual (drop + `event_refs[seq]` decref).
  - newly-acked, preserved → moved out of `event_log` into `preserved_events` *before* the prune runs. The `event_refs[seq]` entry stays.
  - already-past-horizon, preserved → already in `preserved_events` from a prior mutation, untouched by this mutation.
  - already-past-horizon, just-unpreserved → popped from `preserved_events` during the unpreserve phase; `event_refs[seq]` decremented at that point.
- **Outside-session direct load: short_ids only, no records.** Children FxtrIDs in the payload are passed through the escape codec, which calls a new `ShortIDAllocator.get_or_assign_entity_inactive(fxtr_id)` method. Allocates the short_id (run-lifetime stable) but does not mark the key active. No `TrackedEntityRecord` is minted. Children short_ids in the result are descriptors only; if a session wants to load them later, it must first cause them to become active some other way (e.g. via `LoadEntityOp` against the parent on a different short_id, which goes through the active-records path). The user has confirmed this is the expected workflow.
- **Silent load failure path.** Reuse `LoadErrorEvent` — a silent failure still wants the error message on the wire. Only the success path needs a separate event type. `_phase3_commit_load_result` is parameterized on a "success event factory" so silent and non-silent paths share the rest of the pipeline; the error event is shared as-is.
- **Direct load concurrency.** Direct loads run on `state.background_tg`. There is no per-connection serialization; many can race. Read-only at the storage level, so the GIL-atomic registry mutations cover correctness.

## Wire protocol changes

### New mutation ops (extend `MutationOp` union)

- **`PreserveEventsOp(seqs: list[int])`** — preserve each of the listed seqs. Validation: each seq must satisfy the "preserve eligibility" rule above; duplicates within the list are a validation error (a stray duplicate is more likely a client bug than intentional). On commit: each seq is added to `record.preserved_events`. The event's `event_refs[seq]` entry is already populated and just stays.
- **`UnpreserveEventsOp(seqs: list[int])`** — remove each of the listed seqs from `record.preserved_events`. Validation: each must currently be in the working preserved set; duplicates within the list reject. On commit: removed from the set; if the seq is also `<= last_state_ack_seq` after this mutation's ack advance (i.e. past the horizon), the unified retention prune drops it and decref's `event_refs[seq]`. If still in the unacked suffix (impossible given how preserve works, but stated for completeness), the event stays until ack advances normally.
- **`LoadEntitySilentOp(slot, short_id)`** — same op-validation as `LoadEntityOp` (resolve short_id, slot monotonicity). On commit, spawn `load_worker` with a `silent=True` flag (or the equivalent — see "Backend bookkeeping" for the parameterization). Behaves identically to `LoadEntityOp` except phase 3 builds a `LoadResultSilentEvent` instead of `LoadResultEvent`.

### New top-level request (extend `ClientRequest` union)

- **`LoadEntityRequest(request_id, short_id)`** — direct, out-of-band entity load. Not tied to a session. Replies with one of:
  - **`LoadEntityResponse(request_id, payload)`** — escaped CBOR, same wire shape as `LoadResultEvent.payload`.
  - **`LoadEntityErrorResponse(request_id, message)`** — single error reply for any failure: short_id resolution miss, `load_cbor` raised, encode failed, parent record disappeared. `message = repr(exc)` or a short string.

### New session events (extend `SessionEvent` union)

- **`LoadResultSilentEvent(seq, slot)`** — success event for `LoadEntitySilentOp`. No `payload` field. The event still anchors `event_refs[seq]` to the session's in-flight set captured in phase 1 (children get refcounted just like the non-silent variant), so retention, preserve, and resubscribe all flow through the same code paths.

### Snapshot field changes

- **`SessionSnapshotEvent.events`** — semantics extended: now carries preserved events (sorted by seq, ahead of the unacked suffix) followed by `event_log` (the unacked suffix, in seq order). Wire shape unchanged. The existing `event_log`-only monotonicity assertion in `build_session_snapshot` continues to hold against `event_log` (preserved events live in the sidecar dict). Clients distinguish preserved vs. unacked suffix by seq comparison against `last_state.ack_seq`.
- **`SessionDebugSnapshotEvent.preserved_events: list[int]`** — sorted seqs of `record.preserved_events`. New field, default empty list (back-compat for clients that don't render it).

### Server frame union

`ServerFrame` gains `LoadEntityResponse | LoadEntityErrorResponse`. Other types unchanged.

## Backend bookkeeping

### `SessionRecord` additions

- `preserved_events: dict[int, SessionEvent] = field(default_factory=dict)` — events that have been preserved past the ack horizon. Keys are seqs; values are the event structs themselves (moved over from `event_log` at preserve time so `event_log` stays a contiguous unacked-suffix list). The corresponding `event_refs[seq]` entries are *not* moved — they stay in `event_refs` so the existing decref machinery on retention / unpreserve / close continues to work uniformly. Disjoint from `event_log` (an event is in exactly one of the two), and never contains a `StateUpdateEvent`. Always a subset of seqs we have ever produced; cleared on session close.

### Mutation pipeline (`apply_mutation`) changes

The current commit phase order (a) apply patch → (b) tracker adds → (c) attach pre-built callbacks → (d) claim arg refs + spawn workers → (e) tracker removes + close_callback → retention prune → close drain → state-update event → broadcast becomes:

- (a) apply patch — unchanged.
- (b) tracker adds — unchanged.
- (c) attach pre-built callbacks — unchanged.
- (d) claim arg refs + spawn workers — unchanged.
- (e) tracker removes + close_callback — unchanged.
- **(e.5a) Preserve moves.** For each `seq` in `to_preserve` (computed from the working-set diff against `record.preserved_events`): scan `event_log` for the matching event, pop it from `event_log`, store it in `record.preserved_events[seq]`. The corresponding `event_refs[seq]` entry stays untouched.
- **(e.5b) Unpreserve drops.** For each `seq` in `to_unpreserve`: pop from `record.preserved_events`, decref `event_refs[seq]` (per-key decref through `_decref_session_*` for entities and services), pop the `event_refs[seq]` entry. The event itself is past the horizon by definition, so it's discarded — it would have been dropped by retention if not preserved in the first place.
- **Retention prune (unchanged, contiguous-prefix drop).** Walk `event_log` from the front, drop each event with `seq <= ack_seq`, decref `event_refs[seq]` for each. Stop at the first kept event. The existing implementation pattern (`del record.event_log[:drop_count]`) carries over; preserved events are no longer in `event_log` so the contiguous-prefix invariant holds.
- close drain → state-update event → broadcast — unchanged.

### Op validation (dry-run) additions

The op-validation pass (phase 1, sync) runs alongside existing validators:

- A single working set `working_preserved: set[int]` is initialized to `set(record.preserved_events.keys())` at the start of validation and threaded through both op kinds in declaration order so dependent ops (e.g. unpreserve-then-re-preserve) compose cleanly.
- For each `PreserveEventsOp(seqs)`:
  - Reject if `seqs` contains a duplicate.
  - For each `seq`:
    - Reject if already in `working_preserved`.
    - Look up the event in `event_log` (linear scan; `event_log` is small). Reject if not found.
    - Reject if the event is a `StateUpdateEvent`.
    - Reject if `seq > new_ack_seq` (still in unacked suffix; preservation would be a no-op).
    - Add to `working_preserved`.
- For each `UnpreserveEventsOp(seqs)`:
  - Reject if `seqs` contains a duplicate.
  - For each `seq`:
    - Reject if not in `working_preserved`.
    - Remove from `working_preserved`.

At commit time, the diff between `working_preserved` and the original `record.preserved_events` keys gives `to_preserve` and `to_unpreserve` for the (e.5a) / (e.5b) phases above. (Within-mutation `[Preserve(A), Unpreserve(A)]` produces empty diffs — A was never in the original set, was added then removed, so the working set ends unchanged. Note: A *is* in `event_log` initially because the preserve required it, and stays there for the retention prune to drop normally.)

### `LoadEntitySilentOp` plumbing

Each `load_specs` entry gets an extra bool flag (or moves to a tagged shape `LoadSpec(slot, fxtr_id, silent)`). The `load_worker` function gains a `silent` parameter (or, equivalently, an event factory). `_phase3_commit_load_result` is parameterized on a `build_event: Callable[[int, int, Any], SessionEvent]` callback (today implicitly `LoadResultEvent`). Silent path passes a builder that constructs `LoadResultSilentEvent(seq, slot)` and ignores the payload. Phase 1 (attach refs) and the error path (`_deliver_load_error` via `LoadErrorEvent`) are unchanged.

### Outside-session direct load

A new handler in `client_app.py`:

- `_handle_load_entity(state, record, req: LoadEntityRequest)` — sync, validates `short_id` resolves to an active entity (via `state.short_ids.lookup_active_entity`). On miss, sends `LoadEntityErrorResponse`. On hit, spawns `direct_load_worker(state, record, req.request_id, fxtr_id)` on `state.background_tg`.

The worker (`direct_load_worker`):

- Looks up the parent `TrackedEntityRecord` (op-validation guarantees it existed; race possible). On miss, sends `LoadEntityErrorResponse(...)` and returns.
- Opens a transient `load_scope = FxtrScope(isolated=True, mountable=False, include=[parent.scope])`.
- `await load_scope.load_cbor(fxtr_id)`. On exception, sends `LoadEntityErrorResponse(repr(exc))` and returns.
- Escapes the CBOR via `escape(cbor, encode_entity=state.short_ids.get_or_assign_entity_inactive, encode_service=_no_services_in_load)`. The encode callback allocates short_ids with `mark_active=False` for any FxtrID leaf not already known. Service-ref leaves can't appear in CBOR (same as the load worker).
- Sends `LoadEntityResponse(request_id, payload)` via `record.send_nowait`.
- `force_close()`s `load_scope` in `finally`.

If the connection has dropped, `record.send_nowait` may surface a closed-stream error; treat as no-op (the response is just discarded). This mirrors the load worker's `_drop_open_load_slot` defensiveness.

### `ShortIDAllocator` addition

`get_or_assign_entity_inactive(fxtr_id) -> str` — public method that wraps the existing private `_assign_entity(fxtr_id, mark_active=False)`. Allocates the short_id (run-lifetime stable, same as `get_or_assign_entity`) but does *not* add the key to `_active_entity_keys`, so `lookup_active_entity` continues to return `None` for it. `lookup_known_entity` resolves it (matches the existing run-lifetime stability semantics for allocator outputs).

### Snapshot construction

`build_session_snapshot`:
- Existing `event_log`-only assertion (`all(e.seq > record.last_state_ack_seq for e in record.event_log)`) keeps holding — preserved events are in the sidecar dict, not in `event_log`.
- `events` is built as `[<preserved values sorted by seq>] + list(record.event_log)` — preserved events first, then the unacked suffix, all monotonic.

`_build_session_debug_snapshot`:
- Add `preserved_events=sorted(record.preserved_events.keys())` to the constructed `SessionDebugSnapshotEvent`.

### Resubscribe semantics

When a client resubscribes (or another client subscribes for the first time), they receive `last_state` (the most recent `StateUpdateEvent`) plus the full `event_log` in `events`. With preservation, `events` may contain entries with `seq <= last_state.ack_seq` — these are preserved events. The receiving client does whatever bookkeeping it needs (e.g. re-running its own state reduction) using the same logic it would for the original delivery. The `event_refs` refcount contributions are entirely server-side and need no client visibility.

## Substages

### 8a — Preserved events (data model + ops + retention + snapshot)

- Add `preserved_events: dict[int, SessionEvent]` field to `SessionRecord`.
- Add `PreserveEventsOp` / `UnpreserveEventsOp` to `MutationOp` union in `client_protocol.py`.
- Op validation: working-set dry run with all the rules above.
- Commit phase: (e.5a) move preserved events from `event_log` to `record.preserved_events`; (e.5b) drop unpreserved events from `record.preserved_events` (decref `event_refs`); retention prune of `event_log` stays the existing contiguous-prefix pattern.
- Snapshot builder: concatenate `preserved_events.values()` (sorted) with `event_log`. The existing `event_log` assertion stays.
- Add `preserved_events: list[int]` to `SessionDebugSnapshotEvent`; populate from `_build_session_debug_snapshot`. Intermediate snapshot emits during the prune are fine — debug stream is best-effort and small.
- Tests:
  - Preserve a load_result event past horizon; resubscribe; assert event is in `events` with `seq <= last_state.ack_seq`; assert child entity refcount remains.
  - Unpreserve a previously-preserved event; assert event drops from log and child refcount decrements.
  - Within-mutation preserve+unpreserve nets to no-op.
  - Reject preserving a state_update.
  - Reject preserving an unacked-suffix event.
  - Reject preserving an already-preserved event.
  - Reject unpreserving a non-preserved event.
  - Reject duplicate seqs within the same op.
  - Multi-mutation: preserve event 2 with mutation M1 (acks past 2), keep state moving with mutation M2 (does not change preservation), assert event 2 still alive; mutation M3 unpreserves; assert it drops.
  - Debug snapshot includes `preserved_events`.
  - Mixed mutation: TrackEntity + LoadEntity + Preserve (of a prior load result) all succeed in one mutation.
- Update CODE_OVERVIEW.md.

### 8b — Outside-session direct entity load

- Add `LoadEntityRequest` to `ClientRequest` union; add `LoadEntityResponse` / `LoadEntityErrorResponse` to `ServerFrame` union in `client_protocol.py`.
- Add `get_or_assign_entity_inactive` public method to `ShortIDAllocator`.
- Implement `_handle_load_entity` dispatcher in `client_app.py` and `direct_load_worker` in `state.py`.
- Tests:
  - Resolve known active short_id; assert response payload matches what `LoadEntityOp` would produce, but the payload's children short_ids are inactive (`lookup_active_entity` returns None for them).
  - Unknown short_id returns `LoadEntityErrorResponse`.
  - `load_cbor` failure returns `LoadEntityErrorResponse` (e.g. inject a corrupt entity ID).
  - Concurrent direct loads from the same connection don't deadlock and respond independently.
  - No session refcounts are touched: take a session that doesn't pin the parent, do a direct load on the parent (which is pinned by another session), assert no refcount changes on the requesting connection's sessions.
  - Connection drop mid-worker: load completes, response is discarded, no exception escapes the worker.
- Update CODE_OVERVIEW.md.

### 8c — Silent load

- Add `LoadEntitySilentOp` to `MutationOp` union; add `LoadResultSilentEvent` to `SessionEvent` union in `client_protocol.py`.
- Refactor `_phase3_commit_load_result` to take a `build_event` callable; pass `LoadResultEvent`-builder for the existing path and `LoadResultSilentEvent`-builder for the silent path.
- Plumb a `silent` flag through `load_specs`, `load_worker`, and `deliver_load_result_to_slot`.
- Tests:
  - `LoadEntitySilentOp` produces a `LoadResultSilentEvent` with no payload.
  - Side effects identical to non-silent: child entities get tracked + session refcounted; refcounts drop when retention horizon advances.
  - Mixed `[LoadEntityOp, LoadEntitySilentOp]` in one mutation succeeds; slot monotonicity covers them uniformly.
  - Failure path delivers `LoadErrorEvent` (not a silent variant).
  - Silent event participates in preservation (preserve a `LoadResultSilentEvent` from 8a; child refcount stays; resubscribe sees it).
- Update CODE_OVERVIEW.md.

### 8d — Debug UI updates

Light touches to `projects/fxtr_workstation/fxtr_workstation/debug_ui/src/`:
- Render `preserved_events` in the session sub-panel: in the event log, *replace* the gray "past the horizon" coloring with orange (`#c60`) for rows whose seq is in the debug snapshot's `preserved_events` set, and add a `preserved` chip after the row content. Rows past the horizon but not preserved still render gray.
- Render silent loads: the existing `open_loads` slot table is unaffected (silent and non-silent share the same slot machinery). `load_result_silent` events render compactly — one line, no expand affordance, no payload tree.
- No UI for direct-load (it's not a session-scoped operation; the debug UI doesn't observe per-connection RPC-style traffic).
- Update CODE_OVERVIEW.md.

## Tests (cross-cutting)

- **Cache cycle.** Session loads parent E. Session preserves the load_result for E. Session ack's past it. Client uses (b) to fetch E's bytes directly (caches them). Client uses (c) — `LoadEntitySilentOp` — to refresh side effects without retransmission. Assert: child entity refcounts incremented twice (once from preserved load_result, once from silent load), retained correctly across mutations.
- **Preservation interacts cleanly with close.** Session has preserved events, then session is closed. Close drain releases all `event_refs[seq]` contributions (including preserved ones). No leaks.
- **Direct load on entity tracked only via preservation.** Set up: session loads parent, ack's past the load_result, but preserves it (so children stay refcounted). Direct load on a child's short_id succeeds (it's still active via the preserved event's contribution).

## Implementation steps

- [x] **8a.** Preserved events: `SessionRecord.preserved_events`, `PreserveEventsOp` / `UnpreserveEventsOp`, op validation, unified retention prune, snapshot assertion drop, debug-snapshot field. Tests.
- [x] Update CODE_OVERVIEW.md (after 8a).
- [x] **8b.** Direct entity load: `LoadEntityRequest` + responses, `get_or_assign_entity_inactive`, `_handle_load_entity`, `direct_load_worker`. Tests.
- [x] Update CODE_OVERVIEW.md (after 8b).
- [x] **8c.** Silent load: `LoadEntitySilentOp`, `LoadResultSilentEvent`, `_phase3_commit_load_result` parameterization, `load_specs`/`load_worker` plumbing. Tests.
- [x] Update CODE_OVERVIEW.md (after 8c).
- [x] **8d.** Debug UI: preserved-events chip in event log (orange instead of gray, with a `preserved` badge), silent-load row rendering (compact one-liner, no expand affordance).
- [x] Update CODE_OVERVIEW.md (final).

## Open questions

- Cap on the size of `preserved_events`? Phase-0 acceptable to defer; a misbehaving client that preserves indefinitely keeps memory pinned, but session-close drains everything. A future stage could add a per-session cap.
- Pluralized op-name precedent: `PreserveEventsOp` / `UnpreserveEventsOp` use a list shape; future single-shot uses just pass a list of one. We could later retrofit tracker ops the same way for symmetry, but that's a separate cleanup.
- Silent-load implementation strategy: defer to 8c. The escape walk is cheap; if "escape and discard" yields a smaller diff than parameterizing phase 3 around two paths, prefer that. The right shape will fall out of looking at the code.
