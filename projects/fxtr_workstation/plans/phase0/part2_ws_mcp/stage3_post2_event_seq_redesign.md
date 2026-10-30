# Stage 3 post-2 — Event-seq redesign before stage 4

A coordinated change across the workstation (`/client` protocol +
backend) and the MCP client. Tightens the contract for what events
clients can receive, splits the snapshot's events list into two
semantically-distinct buckets, and unifies the client-side handling
of state updates so that the same code path serves both stream
events and snapshot replay.

This work is functionally separable from stage 4 (it doesn't
introduce invoke / retrieve_result), but it's a prerequisite for
stage 4's correctness: stage 4's invoke pipeline relies on a clean
"local view = workstation view + non-state event effects" model,
which the current design wires up only ad-hoc.

## Problem & rationale

Today's MCP client maintains:

- `mirror.last_state`: the most recent `StateUpdateEvent` we've
  applied; treated as the workstation's authoritative view.
- `mirror.data_by_key`: a cached projection of
  `last_state.state["data_by_key"]`.
- `_last_processed_seq`: the highest event seq we've fully applied,
  used as outgoing `ack_seq`.

Stage 4's invoke pipeline forces us to also track local promotions:
when an `InvokeResultEvent` arrives, the workstation's session
state still has `["invoke_pending", N]` for that slot — only future
mutations patch `data_by_key` to `["invoke_result", payload]`. The
local view ought to reflect the promotion immediately so
`retrieve_result` can read it; the workstation view advances later
(when our next mutation flushes the promotion). These two views can
diverge for a while.

This raises three design questions that the current code answers
only implicitly:

1. **Which view is `mirror.data_by_key`?** Workstation's, or
   local? If both, what's the relationship?
2. **What happens on a foreign `StateUpdateEvent` or reconnect?**
   The workstation view changes; our local promotions need to be
   re-derived on top.
3. **What invariants does the wire protocol guarantee?** Today the
   client has to defensively skip a few cases (older
   state_updates in `snapshot.events`, events with seq ≤
   `last_state.ack_seq`) without any way to distinguish "expected
   skip" from "protocol violation."

We resolve this by:

- **Tightening the wire contract.** State updates are only ever
  the authoritative `last_state` field — never inside a
  snapshot's events list. This eliminates the older-state_update
  skip case entirely; if a client ever sees a state_update
  embedded in events, that's a hard protocol error.
- **Splitting the snapshot's events list into two named
  buckets**: `preserved_events` (kept past the ack horizon by
  explicit `PreserveEventsOp`) and `subsequent_events` (the
  unacked suffix). Both contain only non-state events. Each
  bucket has well-defined semantics so the client can validate
  what it receives.
- **Fixing the client-side data model.** `mirror.data_by_key` is
  the **local view**, equal to `last_state.state["data_by_key"]`
  + replayed effects of non-state events with seq >
  `last_state.ack_seq`. Effects are recorded into a connection-
  scoped `_unflushed_state_changes` dict at apply time; replay
  after a state_update reapplies them on top of the new
  workstation view. Outgoing mutations consume
  `_unflushed_state_changes` to emit replace-patches that
  converge the workstation to our local view.

The pay-off is a uniform handler across stream and snapshot, hard
errors for protocol violations, and a straightforward "wipe and
rebuild" recovery story on reconnect.

---

## Part A — Workstation-side changes

The workstation is the authoritative source of session state. The
`/client` wire protocol communicates changes to that state plus
non-state notifications. The redesign clarifies what each frame
shape can carry.

### A.1 Drop state_updates from `event_log`

Today's `SessionRecord.event_log` is a contiguous list of all
events emitted, including state_updates and result events. The
retention prune in `apply_mutation` drops any non-preserved event
with `seq ≤ last_state_ack_seq` from `event_log`; preserved
events go to the `preserved_events` sidecar.

**Change:** state_update events are not stored in `event_log` at
all. They're authoritative through `record.last_state_seq` /
`record.last_state_ack_seq` / `record.state` and broadcast
in-the-moment via `SessionEventFrame`; there's no backing store
for them. `event_log` becomes "non-state events with seq >
last_state_ack_seq".

Implications:
- The `apply_mutation` path: still emits a `StateUpdateEvent` for
  broadcast (live subscribers + retained for `last_state` field).
  Just doesn't append to `event_log`.
- The retention prune path: simplifies — `event_log` is
  non-state-only by construction, so retention only walks
  non-state event refcounts.
- `PreserveEventsOp` already rejects state_update seqs (per its
  current docstring); no change needed.

### A.2 Split the snapshot's events list

`SessionSnapshotEvent`'s `events` field today concatenates
preserved + unacked-suffix into a single list, with the receiver
expected to compare each event's seq against `last_state.ack_seq`
to bucket them.

**Change:** rename / split into two explicit fields, both
non-state-only:

```
class SessionSnapshotEvent:
    subscription_id: int
    session_id: str
    last_state: StateUpdateEvent
    slot_counter: int
    preserved_events: list[NonStateSessionEvent]   # NEW
    subsequent_events: list[NonStateSessionEvent]  # renames `events`
```

- `preserved_events`: events from `record.preserved_events` (the
  existing sidecar dict), sorted by seq. Each has seq ≤
  `last_state.ack_seq` (i.e. before the retention horizon, but
  explicitly preserved).
- `subsequent_events`: events from `record.event_log` (which is
  now non-state-only), sorted by seq. Each has seq >
  `last_state.ack_seq`.

The wire-format gain: `subsequent_events` is exactly the suffix
the receiver should treat as "events delivered since the
mutator's ack" — same shape as live-stream events.

Type alias `NonStateSessionEvent` for the union without
`StateUpdateEvent`:

```
NonStateSessionEvent: TypeAlias = (
    LoadResultEvent | LoadErrorEvent
    | InvokeResultEvent | InvokeErrorEvent
    | CallbackEvent
)
```

### A.3 Add `slot_counter` to `StateUpdateEvent`

```
class StateUpdateEvent:
    seq: int
    ack_seq: int
    state: dict[str, Any]
    closed: bool = False
    tracked_entities: list[str] = []
    tracked_services: list[str] = []
    slot_counter: int = 0   # NEW
```

Set on every state_update broadcast to `record.slot_counter`
post-mutation. Lets clients track the workstation's slot counter
without re-fetching a snapshot.

### A.4 Wire-protocol invariants (post-redesign)

These become hard guarantees the client can rely on:

1. `SessionEventFrame.event` is one of the `NonStateSessionEvent`
   variants OR a `StateUpdateEvent`. (Both are valid in the
   stream.)
2. `SessionSnapshotEvent.preserved_events`: every element is a
   `NonStateSessionEvent` with seq ≤ `last_state.ack_seq`.
3. `SessionSnapshotEvent.subsequent_events`: every element is a
   `NonStateSessionEvent` with seq > `last_state.ack_seq`.
4. State_update events appear in the wire only as
   `SessionSnapshotEvent.last_state` or as the inner event of a
   `SessionEventFrame`. Never inside a snapshot's events
   buckets.
5. State_update events have monotonically-increasing `seq`
   per session.

### A.5 Workstation tests to add / update

- `test_sessions_4b.py` (or stage-equivalent): assert
  `event_log` does not contain state_updates after any commit.
- New: end-to-end test that subscribes to a fresh session,
  commits several mutations, then re-subscribes and asserts the
  snapshot's `last_state` reflects the latest commit and
  `subsequent_events` contains exactly the non-state events
  emitted since.
- New: test that a `PreserveEventsOp` correctly migrates events
  into `preserved_events`, and that those appear in the snapshot's
  `preserved_events` list (not `subsequent_events`).
- Existing tests: any that rely on state_updates appearing in
  `events` will need updates — likely a small number, since the
  client API would naturally read `last_state` for state.

### A.6 Migration

The MCP client is the only known consumer of these fields today.
We coordinate the wire change + MCP update together; no
backwards-compat shim needed.

---

## Part B — MCP client-side changes

### B.1 Data model

Two storage fields on the connection: an authoritative event
list plus a derived view that the mutation builder reads.

```
class WorkstationConnection:
    # … existing fields …

    # The local view of the active session. Holds the data
    # directly — no StateUpdateEvent wrapper.
    self._mirror: SessionMirror

    # Authoritative list of non-state events we've received +
    # processed locally that the workstation hasn't yet subsumed.
    # Every event has seq > mirror.last_state_ack_seq, in seq
    # order. Trimmed when a state_update arrives (drop entries
    # with seq <= new ack_seq). Source of truth for the replay
    # rebuild.
    self._replayable_events: list[NonStateSessionEvent]

    # Derived view: slot_key → (seq, value) recomputed from
    # `_replayable_events`. Same shape and semantics as the
    # current stage-3a impl, so `mutation_builder` doesn't change
    # — but it's now a cached derivation, not a primary store.
    # Recomputed in `_handle_state_update`; updated incrementally
    # in `_apply_state_effect` at event-arrival time.
    self._unflushed_state_changes: dict[str, _UnflushedChange]

    # Workstation's slot counter, copied from
    # StateUpdateEvent.slot_counter. Stage 4 uses this for fresh
    # slot allocation; stage 3 just tracks it (always 0 in stage
    # 3 since no MCP-side op claims slots).
    self._slot_counter: int

    # The highest seq we've fully processed. Used as outgoing
    # ack_seq. Advanced inline by the non-state event arm of
    # `_do_event_job` and by foreign-state_update handling; the
    # self-commit path defers its advance via `_AckCommitJob` to
    # preserve the foreign-events-first queue-ordering property.
    # Never touched inside `_handle_state_update`.
    self._last_processed_seq: int


@dataclass(frozen=True)
class _UnflushedChange:
    seq: int                 # the source event's seq
    value: list[Any]         # the new data_by_key[slot_key] value
```

`SessionMirror` loses its `last_state: Optional[StateUpdateEvent]`
field and becomes a flat data record:

```
@dataclass
class SessionMirror:
    state: dict[str, Any]                # raw workstation state dict
    data_by_key: dict[str, list[Any]]    # local view = state["data_by_key"] + replayed unflushed
    ws_tracked_entities: set[str]
    ws_tracked_services: set[str]
    last_state_seq: int                  # 0 if never set
    last_state_ack_seq: int              # 0 if never set
```

The `data_by_key` field stays as a cached projection of
`state["data_by_key"]`, but now reflects the **local view** —
i.e. the workstation view with replayed unflushed effects
layered on top. The seq fields exist purely for the regression
check + as the "where we are" hint.

`_slot_counter` lives on the connection, not the mirror, because
it isn't part of the session-state JSON and is convenient to
keep alongside `_last_processed_seq` as the two scalar
"position in the seq stream" fields.

### B.2 The handler split

Three primitive operations, each with a single responsibility:

- **`_apply_state_effect(ev: NonStateSessionEvent)`** — handle a
  non-state event arrival.
  - Stage 3 stub: log + drop, do NOT append to
    `_replayable_events`. The MCP never expects non-state events
    in stage 3 (no MCP-side op produces them); foreign clients
    sharing the session could in principle, but no test
    exercises this.
  - Stage 4 will: append `ev` to `_replayable_events`; derive
    the slot_key from `ev.slot` by walking the current mirror's
    `data_by_key` for `["invoke_pending", slot]` /
    `["load_pending", slot]` / …; compute the new value
    (`["invoke_result", payload]` / etc.); update
    `mirror.data_by_key[slot_key]` and
    `_unflushed_state_changes[slot_key]`. Slot lookup must
    succeed (workstation invariant — a result event always
    matches a pending slot); failure raises `ProtocolError`.

- **`_notify_for_event(ev: NonStateSessionEvent)`** — fire the
  channel notification. Stub in stage 3, wired in stage 4.

- **`_handle_state_update(ev: StateUpdateEvent)`** —
  authoritatively reset the mirror, trim the event list, rebuild
  the derived view. **No seq advance.** Caller decides whether
  to advance inline or defer via `_AckCommitJob`.

  ```
  if mirror.last_state_seq and ev.seq <= mirror.last_state_seq:
      raise ProtocolError(...)
  # 1. Update the mirror's data fields.
  mirror.state = ev.state
  mirror.data_by_key = dict(ev.state.get("data_by_key", {}))
  mirror.ws_tracked_entities = set(ev.tracked_entities)
  mirror.ws_tracked_services = set(ev.tracked_services)
  mirror.last_state_seq = ev.seq
  mirror.last_state_ack_seq = ev.ack_seq
  _slot_counter = max(_slot_counter, ev.slot_counter)
  # 2. Trim the event list to events the workstation hasn't
  #    subsumed yet.
  _replayable_events = [e for e in _replayable_events if e.seq > ev.ack_seq]
  # 3. Rebuild the derived view. Slot lookups against the fresh
  #    state may fail if a foreign mutator overwrote the slot_key
  #    — in which case we silently drop the entry (the foreign
  #    write wins, which is correct).
  _unflushed_state_changes = {}
  for e in _replayable_events:
      slot_key = _find_slot_key_for_event(mirror, e)
      if slot_key is None:
          continue
      value = _derive_value_for_event(e)
      _unflushed_state_changes[slot_key] = _UnflushedChange(seq=e.seq, value=value)
      mirror.data_by_key[slot_key] = value
  ```

Different from the pre-refactor design: replay is event-driven,
not value-cached. Slot-key lookup happens against the fresh
mirror state, so a foreign overwrite is correctly detected and
the stale local promotion is dropped instead of clobbering the
foreign write.

`_handle_state_update` is called from three places:

1. **Foreign state_update arm of `_do_event_job`.** Caller
   advances `_last_processed_seq` inline after:
   ```
   _handle_state_update(ev)
   _last_processed_seq = max(_last_processed_seq, ev.seq)
   ```
2. **Snapshot bootstrap inside `_do_subscribe`.** Same as
   foreign — inline advance after.
3. **Self-commit synthesis in `_apply_local_commit`.** Caller
   does NOT advance inline; `_apply_script_mutation` enqueues an
   `_AckCommitJob(synthesized.seq)` so the advance lands behind
   any foreign events queued during the response await.

Non-state event arm of `_do_event_job`:

```
# Hard-error checks (B.5).
if ev.seq <= _last_processed_seq: raise ProtocolError(...)
if ev.seq <= mirror.last_state_ack_seq: raise ProtocolError(...)
_apply_state_effect(ev)
_notify_for_event(ev)
_last_processed_seq = max(_last_processed_seq, ev.seq)
```

### B.3 Subscribe-time handling

`_do_subscribe` runs on both initial subscribe and reconnect.
Same path either way — throw away per-session local state and
rebuild from the snapshot.

```
# Step 1: wipe.
self._mirror = SessionMirror()  # all defaults / empty
self._replayable_events = []
self._unflushed_state_changes = {}
self._last_processed_seq = 0
self._slot_counter = 0

# Step 2: validate + log+skip preserved_events. They're subsumed
# by last_state.state.
for ev in snapshot.preserved_events:
    assert ev.seq <= snapshot.last_state.ack_seq  # else ProtocolError
    _log.debug("preserved event seq=%d kind=%s; skipping", ...)

# Step 3: apply last_state and inline-advance seq.
self._handle_state_update(snapshot.last_state)
self._last_processed_seq = max(
    self._last_processed_seq, snapshot.last_state.seq
)

# Step 4: enqueue subsequent_events as _EventJobs. They drain
# through the standard non-state arm, each advancing seq.
for ev in snapshot.subsequent_events:
    assert ev.seq > snapshot.last_state.ack_seq  # else ProtocolError
    self._job_send.send_nowait(_EventJob(frame=SessionEventFrame(
        subscription_id=sub_id, event=ev,
    )))
```

The no-prior-state branch of `_handle_state_update` reads
`mirror.last_state_seq == 0` and applies cleanly — no special
case needed.

**Why this works.** Local state we throw away is all derivable:

- Pre-disconnect promotions for events the workstation hasn't
  subsumed are in `snapshot.subsequent_events` — replayed via
  the enqueued `_EventJob`s, which re-run
  `_apply_state_effect` and produce fresh
  `_unflushed_state_changes` entries.
- Pre-disconnect promotions that a foreign mutator subsequently
  promoted for us are reflected in `snapshot.last_state.state`
  directly — they don't need replay.

### B.4 Mutation builder

`mutation_builder.build_mutate_request` keeps its current shape
and contract — it consumes `_unflushed_state_changes` (the
derived dict) to emit pre-flush `replace` patches before the
script's queued ops. No semantic change. The fact that the
dict is now derived from a separate event list is invisible to
the builder.

After commit, the synthesized `StateUpdateEvent` is passed
through `_handle_state_update`, whose trim+rebuild clears the
just-flushed events from both `_replayable_events` and
`_unflushed_state_changes`.

### B.5 Hard-error cases

Promote each silent-skip case to a logged error + supervisor
exit. The supervisor exit lets the agent retry from a fresh
subscribe, giving a clean recovery rather than carrying
corrupted local state.

| Condition | Action |
|---|---|
| `StateUpdateEvent.seq <= mirror.last_state_seq` (regression) | error |
| `StateUpdateEvent` appears in `snapshot.subsequent_events` or `snapshot.preserved_events` | enforced by the wire's `NonStateSessionEvent` typing — msgspec rejects at decode time |
| Non-state event with `seq <= mirror.last_state_ack_seq` arrives in stream | error |
| Non-state event with `seq > mirror.last_state_ack_seq` in `snapshot.preserved_events` | error |
| Non-state event with `seq <= mirror.last_state_ack_seq` in `snapshot.subsequent_events` | error |
| Within a single cycle, event seq ≤ `_last_processed_seq` (within-cycle re-delivery) | error |
| Stage 4: result event whose `slot` doesn't match an `["invoke_pending", N]` (or `load_pending` etc.) entry in `mirror.data_by_key` at arrival time | error |

### B.6 Tests

- **Unit tests** (synthetic event streams; no wire):
  - `_handle_state_update` updates the mirror, drops subsumed
    entries from `_replayable_events`, and rebuilds the derived
    `_unflushed_state_changes` dict correctly. No seq advance.
  - The non-state event arm of `_do_event_job` advances seq
    after `_apply_state_effect` (using a stub event-kind that
    we wire just enough to exercise the path, or by direct
    construction).
  - Replay-after-foreign-overwrite: process synthetic event X
    that promotes slot K (rebuilds dict with K); apply a
    state_update whose `state.data_by_key[K]` is something
    else; assert the rebuild silently drops the stale entry.
    (Stage 4 will have real coverage once invokes are wired.)
  - Hard-error cases: each row from B.5 raises the right error
    (excluding stage-4-specific rows).
- **Integration test**: foreign state_update mid-script (already
  exists; should keep working under the new model).
- **Integration test**: reconnect mid-pending — defer to stage
  4 once invokes can produce real non-state events.

---

## Implementation order

Workstation first (since the client depends on it):

- [x] Workstation A.3: add `slot_counter: int` to `StateUpdateEvent`.
      (Smallest change; lands first.) Also dropped the redundant
      top-level `slot_counter` from `SessionSnapshotEvent` — it now
      lives on `last_state` only.
- [x] Workstation A.1: drop state_updates from `event_log`.
      Retention prune already only walked refs; no behavior change
      there. Deleted the dead PreserveEventsOp `isinstance(StateUpdateEvent)`
      validation branch (unreachable now). `SessionRecord.event_log`
      / `preserved_events` typed as `NonStateSessionEvent`.
- [x] Workstation A.2: split `SessionSnapshotEvent.events` into
      `preserved_events` + `subsequent_events`. Added
      `NonStateSessionEvent` type alias. Updated
      `build_session_snapshot`.
- [x] Workstation tests (A.5). Updated existing tests
      (`test_sessions_3b1` retention case, `test_sessions_3a`
      initial snapshot, `test_sessions_8a` preserve, `test_sessions_8c`
      silent preserve, `test_client_protocol_unit` wire shapes).
      Added `test_event_log_excludes_state_updates` and
      `test_resubscribe_reflects_latest_commit_in_last_state` in
      `test_sessions_3b1`. Updated debug UI TypeScript types +
      handler logic. All 329 tests pass; debug UI builds cleanly.

Then the MCP client:

The first pass of Part B (described in earlier revisions of
this file) landed with a value-cached unflushed dict and a
``advance_last_processed_seq`` flag on the unified handler.
After review (see commit history / the conversation around the
flag), the design was revised to: separate authoritative event
list + derived dict, no flag, mirror flattened (no
``last_state`` event wrapper). The redo is being done in-place;
checkboxes below reflect the **revised** B implementation.

- [x] B.1: add `_replayable_events: list[NonStateSessionEvent]`
      and `_slot_counter: int` fields. Flatten `SessionMirror`
      (drop `last_state: Optional[StateUpdateEvent]`; add
      `state` / `last_state_seq` / `last_state_ack_seq`).
      `UnflushedChange` + `_unflushed_state_changes` retained
      (now a derived view).
- [x] B.2: split helpers — `_apply_state_effect(ev)`,
      `_notify_for_event(ev)`, `_handle_state_update(ev)`
      (mirror update + trim + rebuild, **no seq advance**).
      Stage-3 stub of `_apply_state_effect` always appends to
      `_replayable_events` and then raises
      `NotImplementedError` for every kind (per-kind dispatch
      inline — stage 4 fills in real branches).
- [x] B.3: `_do_subscribe` wipes local state, validates buckets,
      applies `last_state` via `_handle_state_update`,
      inline-advances `_last_processed_seq`, enqueues
      `subsequent_events` as `_EventJob`s.
- [x] B.4: `mutation_builder.build_mutate_request` consumes the
      derived `_unflushed_state_changes` as before.
      `_apply_local_commit` updates the mirror directly (see
      "Follow-up cleanups" below).
- [x] B.5: hard-error invariants in place.
      `_dispatch_job` is bare; per-tool-call wrappers
      (`_handle_open_session_job` / `_handle_close_session_job` /
      `_handle_script_job`) catch their tool-error types
      (`SessionAlreadyActive` / `NoActiveSession` /
      `WorkstationRejected`); anything else tears down.
      Introduced `WorkstationRejected` for workstation-said-no
      errors; converted the protocol-violation `RuntimeError`s
      to `ProtocolError`.
- [x] B.6: tests reworked for the new shape; full coverage
      including the rebuild path with an injected stage-4-shaped
      handler. `_fail_job` removed; the queue-drain at supervisor
      exit inlines its type dispatch.
- [x] Update `CODE_OVERVIEW.md` for the revised data model.

## Follow-up cleanups (3-post-3)

Three small cleanups caught during review of the implementation:
the self-commit synthesis was a code smell, and two pieces of
infrastructure useful for stage 4 are cheap enough to land now.

- [x] **Drop self-commit synthesis.** `_apply_local_commit` no
      longer constructs a fake `StateUpdateEvent` to feed into
      `_handle_state_update`. The workstation self-suppresses
      our own state_update broadcast, and we know everything we
      need from the response + `BuildResult` directly. By
      construction our outgoing `ack_seq = _last_processed_seq`
      covers every event in `_replayable_events` (because
      `_apply_state_effect` is the only writer of the list and
      `_do_event_job` advances `_last_processed_seq` past every
      event it processes) — so the trim drops everything and the
      rebuild would iterate zero times. The new
      `_apply_local_commit` just updates the mirror via
      `apply_workstation_view` and clears
      `_replayable_events` + `_unflushed_state_changes` + a
      prune of `_notified_seqs` (see below).
- [x] **Mirror primitive rename.** `SessionMirror.reset_from_state_update(ev)`
      → `apply_workstation_view(state, last_state_seq,
      last_state_ack_seq, tracked_entities, tracked_services)`.
      Takes raw values rather than an event. The two callers
      (`_handle_state_update` for foreign state_updates,
      `_apply_local_commit` for self-commits) each pass the
      args from their respective source.
- [x] **`pending_slot_index` reverse lookup.**
      `SessionMirror.pending_slot_index: dict[int, str]` — maps
      `slot` to `slot_key` for entries whose value is
      `["invoke_pending", slot]` or `["load_pending", slot]`.
      Maintained by `apply_workstation_view` (rebuilt from
      scratch on each mirror reset). Lets stage 4's
      `_apply_state_effect` find slot_keys for arriving result
      events in O(1).
- [x] **`_notified_seqs` dedupe for `_notify_for_event`.**
      Connection field tracking seqs we've already delivered as
      channel notifications. `_notify_for_event` checks + adds
      before delivering — suppresses redelivery from rebuild
      loops, foreign-state-update reapplies, and snapshot
      `subsequent_events` after reconnect. Pruned alongside
      the ack horizon in `_handle_state_update` and
      `_apply_local_commit`. **Reset only on session close**
      (and at connection construction) — explicitly NOT wiped
      in `_do_subscribe`'s reconnect path, since persistence
      across reconnects is the whole point: re-delivered events
      from a snapshot's subsequent_events get suppressed if we
      already notified the agent before the disconnect.

## Out of scope

- Stage 4's invoke / retrieve_result builtins — they ride on
  this redesign but are their own plan.
- Channel notifications wiring (`notify_channel` callback,
  `server.py` plumbing) — same.
- Idle-flush of `_unflushed_state_changes` when no agent activity
  for a while. Future-stage feature, not needed for v1.
- Surfacing `preserved_events` to the agent as a history view.
  Future stage if useful.
