# Stage 4 — Sessions in the debug UI

Goal: extend the stage-2 `debug_ui` with a Sessions view that mirrors
the part-0 sketch — a sessions list with checkboxes, and a per-session
sub-panel showing the state, the event-log timeline (with the
"committed-twice" rendering), and the session's tracked entities /
services. Read-only — no mutation panel yet.

This stage also takes the opportunity to tidy up the resource-
subscription wire shape that stage 2 introduced. The current "full
snapshot on every change" pattern doesn't separate production-needed
information (add/remove/bind-name) from debug-only information
(`pinned_by_sessions`, `registering_connection_ids`, refcount
breakdowns), so every session pin change fans out a complete
re-broadcast of the entities/services lists. Stage 4 collapses this
into a snapshot+delta pattern with an explicit `debug` flag, and
folds session listing into the same resource-subscription machinery.

The end-to-end deliverable is a new interactive demo script (sibling
to `examples/debug_ui_demo.py`) that lets the user pick from a small
menu of scenarios and walks through each one prompt-by-prompt, with
one prompt between every observable op so nothing slips by in a
single render cycle. The demo doubles as the manual QA story for this
stage — there are no automated tests beyond what the existing 3a–3d
test suites already cover, plus targeted unit tests for the new wire
shapes.

See `../sketch.md` for the broader workstation vision and
`./overview.md` for the part-1 framing.

## Scope (resolved)

- Sessions panel rendered as a fourth section appended below the
  existing rpc / services / entities tables. No tabs, no rail; the
  page is one long column. Each checked session inflates an inline
  sub-panel directly underneath the list.
- Per-session sub-panel renders: collapsible JSON state, event-log
  timeline using the "content-after-ack, marker-at-seq" layout
  described under "Event log rendering" below, and the session's
  pins as a filtered slice of the global services / entities tables
  with refcount badges sourced from a parallel debug subscription.
- Session-list filter: client-side substring filter over metadata's
  stringified JSON. Server-side `filter_metadata` is punted — no
  consumer needs it yet.
- No mutation UI in this stage. Mutations come from the demo script
  (and later from the agent / full UI clients).

## Resource subscription redesign (shared by 4a)

The four resources after stage 4 are:

```
rpc_connections | services | entities | sessions
```

The bespoke `list_sessions` and `subscribe_session_list` ops go
away — sessions become a regular resource. (`subscribe_session` /
`unsubscribe` and the new `subscribe_session_debug` are *not* part of
the resource-subscription machinery; they're separate per-session
streams. Resource subscriptions only ever return collection-level
shapes.)

### Wire pattern: snapshot then deltas

Each subscription emits one initial `resource_snapshot` carrying the
full record list, then a stream of `resource_changes` frames as the
collection mutates:

```
{type: "resource_snapshot", subscription_id, data: [<record>, ...]}
{type: "resource_changes", subscription_id,
   added:    [<record>, ...],
   updated:  [<record>, ...],
   removed:  [<key>, ...]}
```

Adds/updates carry the full record; removes carry just the record's
key (`short_id` for services/entities, `connection_id` for
rpc_connections, `session_id` for sessions). A change frame batches
everything that happened at the boundary of a single sync mutation —
i.e. one frame per state.py mutation, not per individual field.

No sequence numbers — WebSocket guarantees per-connection ordering
and we don't need ack/replay for resource subscriptions. Sequence
numbers stay scoped to the session event log, which is the only
place they're load-bearing.

### `debug` flag

`SubscribeRequest` gains `debug: bool = False`. In non-debug mode:

- Records exclude debug-only fields. For the four resources today:
  - `services` / `entities`: drop `registering_connection_ids` and
    `pinned_by_sessions`.
  - `rpc_connections`: drop `service_registration_short_ids` and
    `entity_registration_short_ids`.
  - `sessions`: TBD during 4a — provisionally identical for now;
    flag accepted for forward compat.
- The server only emits a `resource_changes` frame to a non-debug
  subscriber when a non-debug-visible field changes. Debug-only
  field changes (e.g. an entity's `pinned_by_sessions` ticking up
  when a session pins it) are suppressed for non-debug subscribers.

This is the change that buys the most: production session pin
churn no longer fans out to every entities/services subscriber.

The debug UI subscribes everywhere with `debug=true`.

### Session list as a resource

`subscribe(resource="sessions")` returns one record per session,
lifecycle-only:

```
{session_id, metadata, created_at, closed: false}
```

State-progression fields (`last_state_seq`, `last_state_ack_seq`,
`slot_counter`) are deliberately omitted — they tick every
mutation, which would make sessions resource noisy for every
subscriber. Live progress for a specific session is available via
`subscribe_session` (event log) and `subscribe_session_debug`
(refcounts + slot_counter, see below).

The `debug` flag is accepted on `subscribe(resource="sessions")`
for forward compat but is a no-op for now — there's no per-session-
record debug field worth carrying that isn't already on the
dedicated debug subscription.

### Change-detection implementation

`state.py` already has notification hooks at every mutation site
(`notify_*_subscribers` patterns). The redesign rewires those hooks
to emit `(added, updated, removed)` deltas directly rather than
re-running the full snapshot builder. Concrete shape:

- A small `ResourceChangeBuffer` collected during the synchronous
  portion of each mutation, recording (kind, action, key) tuples.
- At the end of the mutation, the buffer is flushed: per
  subscription, it's filtered against debug/non-debug visibility,
  collapsed (an add+remove of the same key cancels), and pushed as
  one `resource_changes` frame (skipped if empty after filtering).
- Snapshot builders stay around — they're called once on subscribe
  to build the initial frame, and could be reused as a fallback if
  we ever need to issue a fresh snapshot mid-stream (not planned).

## Event log rendering (resolved)

For each `state_update` event with `seq=N`, `ack_seq=A`, the timeline
shows two rows:

- **Pending row (sender's view).** Rendered between the row at seq=A
  and the row at seq=A+1 — i.e. immediately after the position the
  mutator was acked-up-to when they sent it. Carries the full
  state-update payload: post-state (collapsible), `update_id`, a
  small "(committed at N)" annotation linking down to its ack row.
- **Committed marker (server's view).** Rendered at the timeline row
  for seq=N. Minimal one-line marker — no payload, just
  "state_update committed (acked up to A)".

Result events (`load_result`, `load_error`, `invoke_result`,
`invoke_error`) render once at their `seq`.

Anything with `seq <= last_state_ack_seq` (the most recent state
update's ack) is rendered grayed-out — those events have either been
folded into state by the mutator or are strictly older than the
current ack horizon.

The `last_state` from `SessionSnapshotEvent` participates in the same
rendering: its pending row sits between row `last_state.ack_seq` and
the next event, and its committed marker sits at row `last_state.seq`.
Whether `last_state` is also present inside `events` is verified
against the wire shape during 4e; if so, fold-dedupe in the UI.

## Per-session debug subscription

A new op `subscribe_session_debug(session_id)`, mirroring the shape
of `subscribe_session`, delivers refcount + slot_counter data on a
parallel stream:

```
{type: "session_debug_snapshot", subscription_id, session_id,
   entity_refcounts: {<short_id>: <count>},
   service_refcounts: {<short_id>: <count>},
   slot_counter: <int>}
```

`slot_counter` rides along on the same channel — it's another
internal-state field that the debug UI wants to expose but doesn't
belong on the public resource subscription. It bumps on every
load/invoke op apply, so it tracks the same "internal session
churn" the refcounts do.

The frame is sent once on subscribe and re-sent (full snapshot, not
delta) on every refcount or slot_counter mutation. The `_incref/_decref_session_X`
primitives are already the choke point for refcount changes — they
gain a hook that re-emits the snapshot to any subscribers. Full
snapshot rather than delta because:

- The data is small (refcount dicts per session — typically dozens
  of keys at most, in practice often empty or single-digit).
- The simplification matters: no diff bookkeeping, no per-key seq
  numbers, no edge cases.
- This is a debug subscription; cost is not a concern.

The pin-set fields (`tracked_entities` / `tracked_services` — the
explicit pins, distinct from refcount keys) are already exposed
elsewhere: they only change via mutation ops, so they're added to
`StateUpdateEvent` directly (see 4b). The session_debug snapshot
focuses purely on the refcount picture.

## Session-side wire additions

`StateUpdateEvent` gains:

- `tracked_entities: list[str]` — explicit-pin entity short_ids.
- `tracked_services: list[str]` — explicit-pin service short_ids.

These match the session's `tracked_entities` / `tracked_services`
sets *after* the mutation commits (post-tracker-ops, post-close-drain
for terminal closes). They're absent from result events / error
events because those don't change the explicit pin sets.

The `SessionSnapshotEvent` itself does not need new pin fields — its
`last_state` is a `StateUpdateEvent` and already carries them.

## Substages

After 4a/4b landed, the remaining work groups naturally into three
checkpoints:

- **4c** — `ws_client.ts` rewrite + Sessions panel with stub sub-panel.
- **4d/e** (merged) — fleshed-out per-session sub-panel: state tree +
  pins view (was 4d) and event-log timeline (was 4e). They share the
  sub-panel container and one JSON-tree component, so they land
  together.
- **4f** — scenario-picker demo script.

Order matters: 4c's TS work talks to the 4a wire shape and 4b session
debug stream; 4d/e renders them.

### TS client simplifications (resolved with Daniel)

- Each `subscribe(...)` exposes only an `onSnapshot` callback (full
  merged data after deltas applied). No raw `onChanges` surface — the
  delta merge is internal.
- `subscribeSession(session_id)` maintains an internal
  `{lastState, slotCounter, events}` snapshot and re-fires its
  `onSnapshot` whenever any of them change. `subscribeSessionDebug`
  re-fires whenever a fresh `SessionDebugSnapshotEvent` arrives.
- Per-resource primary-key field is hardcoded in the TS client
  (`connection_id` for `rpc_connections`, `short_id` for
  `services`/`entities`, `session_id` for `sessions`).
- Ack frames are `console.log`'d defensively (the debug UI never
  sends mutations, so they shouldn't arrive in practice).

### 4a — Resource subscription overhaul (snapshot + deltas + debug flag)

- Wire-protocol changes in `client_protocol.py`:
  - Add `debug: bool = False` to `SubscribeRequest`.
  - New `ResourceChangesEvent` server frame (tagged `"resource_changes"`,
    carries `subscription_id` + `added` / `updated` / `removed`).
    Rename current `SnapshotEvent` to `ResourceSnapshotEvent` (tag
    `"resource_snapshot"`) for symmetry — keep an alias if it
    simplifies the diff.
  - Extend `ResourceKind` literal with `"sessions"`.
  - Remove `ListSessionsRequest` / `SubscribeSessionListRequest` /
    `SessionListResponse` / `SessionListSnapshotEvent` from the
    protocol.
- `state.py`:
  - Add a `sessions` resource path: snapshot builder + per-mutation
    change emission. Decide on the record shape (lifecycle-only vs.
    include `last_state_seq` etc.) — see "Session list as a
    resource" above.
  - Refactor each resource's notification hook to emit
    `(added, updated, removed)` deltas. The simplest implementation:
    a per-mutation change buffer that records key-level events;
    flushed at the end of the mutation, filtered per subscription,
    pushed as one frame.
  - Per-subscription state gains a `debug: bool` field. Snapshot
    builders take it as a parameter and project records accordingly;
    delta filters use it to suppress debug-only-field changes.
- `client_app.py`: replace the bespoke session-list handlers with
  the unified resource subscription path. Tests using
  `list_sessions` / `subscribe_session_list` move to
  `subscribe(resource="sessions")`.
- Test updates:
  - `test_snapshot_builders.py`: split into snapshot tests + delta
    tests. New cases for non-debug projections + debug-only-change
    suppression.
  - `test_client_protocol.py` / `test_client_protocol_unit.py`:
    update for the new wire shapes.
  - `test_sessions_3a.py`: rewrite the list-related tests to use
    the resource subscription.
  - `test_sessions_3d.py`: update the `pinned_by_sessions` tests to
    expect deltas + the debug flag.
  - New: a small dedicated test exercising the snapshot+delta path
    end-to-end on the entities resource (subscribe, register,
    unregister, drop the connection — assert frames in order).
- Update CODE_OVERVIEW.md.

### 4b — Session-side: debug subscription + pin-set fields on state_update

- Add `tracked_entities: list[str]` / `tracked_services: list[str]`
  to `StateUpdateEvent`. Populate at the construction site in
  `apply_mutation` (after tracker ops + retention prune; for terminal
  closes, both are empty by drain-time).
- New op `SubscribeSessionDebugRequest{request_id, session_id}` and
  new server frame `SessionDebugSnapshotEvent{subscription_id,
  session_id, entity_refcounts, service_refcounts, slot_counter}`.
- Per-connection subscription union grows a `SessionDebugSubscription`
  variant.
- Hook `_incref_session_entity` / `_decref_session_entity` (and the
  service variants) to re-emit a fresh `SessionDebugSnapshotEvent`
  to any active subscribers for that session. Also fire on
  session-close drain and on the slot-counter bump in
  `apply_mutation`'s slot-op apply phase.
- Tests:
  - `StateUpdateEvent.tracked_entities` correctly reflects pin set
    after a `track_entity` op, after `untrack_entity`, after close.
  - `subscribe_session_debug` returns initial snapshot; subsequent
    refcount changes (explicit pin, retained-event drop, in-flight
    delivery promote/rollback) push fresh snapshots.
  - Subscription teardown on session-close.
- Update CODE_OVERVIEW.md.

### 4c — TS client + Sessions panel (stub sub-panel)

- Rewrite `ws_client.ts`:
  - Replace single-snapshot subscription model with snapshot+deltas:
    each `subscribe` returns a small handle with an `onSnapshot`
    callback (full data) and an `onChanges` callback (added /
    updated / removed). The internal book-keeping applies deltas
    against a local copy and re-fires `onSnapshot` post-merge for
    convenience consumers, so the App can stay reactive without
    explicit delta-handling.
  - Add `debug` flag to subscribe.
  - Add `subscribeSession(session_id)` and
    `subscribeSessionDebug(session_id)` methods returning per-stream
    handles (frame callbacks).
  - **Fix the pre-existing field-naming drift between `ws_client.ts`
    and the Python wire.** The TS types currently use `id` on
    `SubscribeRequest` / `UnsubscribeRequest` / `AckResponse` /
    `ErrorResponse`, but the Python protocol uses `request_id` on
    requests/acks/errors and `subscription_id` on subscription-bound
    frames. The handler reads `frame.id`, which is `undefined` on
    every wire frame. This drift was introduced after the original
    debug-UI landed and was masked by the lack of an end-to-end
    test against the real server. The 4c rewrite needs to align the
    TS field names with the Python wire (and ideally land a small
    smoke test that fails on a regression).
- `App.tsx`:
  - Subscribe to `rpc_connections` / `services` / `entities` /
    `sessions` all with `debug: true`.
  - New `SessionsPanel` component appended below the existing
    tables. Renders the sessions list with checkboxes, a small
    metadata-substring filter input, and inflates a stub sub-panel
    per checked session showing just the session UUID +
    `last_state.seq` / `ack_seq` placeholder.
- Update CODE_OVERVIEW.md.

### 4d — Per-session sub-panel: state tree + pins view (driven by debug subscription)

- Render `last_state.state` as a collapsible JSON tree, starting
  collapsed. Small recursive component, no third-party lib. Updates
  whenever a new `state_update` arrives.
- "Session pins" sub-section, two small tables (entities / services):
  - Rows filtered from the global tables by `pinned_by_sessions`
    membership.
  - Refcount badge per row sourced from
    `SessionDebugSnapshotEvent`'s refcount dicts.
  - Source label per row: `explicit pin` if the short_id appears in
    `last_state.tracked_*`, otherwise `retained-event only`. (Both
    if it's in the explicit pin set *and* refcount > 1, but flag
    that as a stretch — single label is fine for v1.)
- Update CODE_OVERVIEW.md.

### 4e — Event-log timeline rendering

- The timeline panel is a vertical list ordered by seq. Build by:
  - Folding `last_state` (if seq > 0) into the events list,
    deduping if it's also present in `events`.
  - For each `state_update`, emit two virtual rows: a pending row
    at sort-key `(ack_seq, "pending", insertion_order)` and a
    committed-marker row at `(seq, "committed", insertion_order)`.
  - For each result/error event, emit one row at sort-key
    `(seq, "single", insertion_order)`.
  - Sort and render.
- Pending rows render the full payload (state tree, update_id,
  "committed at N" annotation). Committed-marker rows render a
  one-line "state_update committed seq=N (acked up to A)". Result
  rows render slot + escape-decoded payload preview / error message.
- Anything with `seq <= last_state_ack_seq` renders grayed-out.
- Pending rows whose ack_seq is past the retention horizon may
  have no committed marker available in the visible log — render
  the pending row, no marker. (Already gray-zone anyway.)
- Update CODE_OVERVIEW.md.

### 4f — Demo script with scenario picker

A new file `examples/debug_ui_sessions_demo.py`, sibling to the
existing `debug_ui_demo.py`. Boots a workstation with the bundled
debug UI, prints the URLs, and presents an interactive menu:

```
Pick a scenario:
  1. Basic happy path (RPC peer → client → state mutation, two subscribers)
  2. Slot delivery (load + invoke; entity & service refs surfaced)
  3. Tracker keeps RPC alive (session pins service, RPC drops)
  4. Stale-ack rejection + same-mutator skip-ahead (two clients)
  5. Retention horizon (load surfaces child, ack past it)
  6. Close-as-mutation
  q. Quit
```

After each scenario completes the menu re-prompts so the user can
pick another against the same workstation. Each scenario lives as
its own coroutine; common fixtures (RPC peer with EchoImpl,
Note/NoteMaker/EchoFactory exposes) are factored into small helpers
local to the demo file. Reuse `tests/_ws_client.py::ClientWS` for
the client-side WS, `expose_service` / `expose_entity` for the RPC
side.

Each scenario inserts a `prompt(...)` between every observable op so
the user can watch the UI tick forward in single steps. Aim for
~8–15 prompts per scenario.

Skeleton per scenario (taking #4 — stale-ack — as the most
illustrative):

```
async def scenario_stale_ack(ws):
    async with connect_client(ws.client_url) as alice:
        async with connect_client(ws.client_url) as bob:
            await prompt("alice creates a session")
            sid = await create_session(alice, metadata={"name": "stale-ack"})
            await prompt("alice subscribes")
            await subscribe_session(alice, sid, sub_id=1)
            await prompt("bob subscribes")
            await subscribe_session(bob, sid, sub_id=10)
            await prompt("alice mutates: set state.foo=1, ack_seq=0")
            await mutate(alice, sub_id=1, ack_seq=0, update_id=1, patch=[ADD foo=1])
            await prompt("bob (still ack=0) mutates → expect rejection")
            await mutate(bob, sub_id=10, ack_seq=0, update_id=1, patch=[ADD bar=2])
            await prompt("alice mutates again at ack=0 → expect skip-ahead success")
            await mutate(alice, sub_id=1, ack_seq=0, update_id=2, patch=[ADD baz=3])
            await prompt("bob acks up to seq=2, retries → succeeds")
            await mutate(bob, sub_id=10, ack_seq=2, update_id=2, patch=[ADD qux=4])
```

Helpers wrap the typed `client_protocol` request structs; their
*responses* are printed (decoded `ServerFrame`) so the user sees the
wire exchange in the terminal alongside the UI updates. Foreign
subscribers' incoming frames are read from a small background task
group draining each client's `receive()` loop into stdout, started
at the top of each scenario and torn down at the bottom.

Sequence sketches for the other scenarios live as inline comments
in the demo file. They're straightforward combinations of the
per-stage tests we already have:

- **#1 happy path**: RPC peer exposes service+entity → alice creates
  + subscribes session → bob subscribes → alice mutates patch →
  bob's foreign subscriber prints `state_update`.
- **#2 slot delivery**: track_entity, then `load_entity` →
  `load_result` arrives at slot → `invoke` make_pair →
  `invoke_result` with entity+service tags surfaces, debug UI shows
  new tracked entries.
- **#3 tracker-keeps-rpc**: session.track_service the exposed
  echo → RPC peer disconnects → debug UI shows the service still
  tracked.
- **#5 retention horizon**: load `NoteWithChild` → child appears
  in `entity_refcounts` (visible via session debug subscription
  badge in UI) → empty mutation with ack_seq past the load result
  → child refcount drops, child released.
- **#6 close**: close-mutation, terminal state_update
  (`closed=true`) delivered to foreign subscriber, session removed
  from the resource list.

- Update CODE_OVERVIEW.md.

## Implementation steps

- [x] **4a.** Resource subscription overhaul (snapshot+deltas, debug
      flag, sessions-as-resource).
- [x] Update CODE_OVERVIEW.md (after 4a).
- [x] **4b.** Session-side wire: pin-set fields on `StateUpdateEvent`
      + `subscribe_session_debug` op + `SessionDebugSnapshotEvent`.
- [x] Update CODE_OVERVIEW.md (after 4b).
- [x] **4c.** TS client + Sessions panel (stub sub-panel).
- [x] Update CODE_OVERVIEW.md (after 4c).
- [x] **4d/e (merged).** Per-session sub-panel: state tree + pins
      view + event-log timeline.
- [x] Update CODE_OVERVIEW.md (after 4d/e).
- [x] **4f.** `examples/debug_ui_sessions_demo.py` with scenario
      picker (all 6 scenarios).
- [x] Update CODE_OVERVIEW.md (final).

## Open questions to revisit

- **`last_state` duplication** (4e): the wire shape has `last_state`
  in the snapshot; `events` may or may not also include it. Verify
  during 4e and dedupe client-side if needed.
- **Pending-row → committed-marker hyperlink** (4e): optional UX
  nicety; punt unless asked for during review.
- **Server-side `filter_metadata`**: punted; revisit when the agent
  MCP or full UI client surfaces a real consumer.
