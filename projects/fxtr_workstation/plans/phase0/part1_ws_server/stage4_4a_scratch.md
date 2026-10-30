# Stage 4a scratch — resource subscription overhaul

Working notes for the 4a substage. Not the final plan; once the
shape is settled here, the relevant pieces fold into
`stage4_debug_ui_sessions.md`.

Scope (settled with Daniel):

- snapshot+delta wire shape, per-mutation deltas, debug flag.
- sessions become a regular resource (`add` / `remove` only — no
  updated records, no `closed` field; close = remove).
- `SnapshotEvent` renamed in place to `ResourceSnapshotEvent`; no
  alias. New `ResourceChangesEvent`. `ResourceKind` literal grows
  `"sessions"`.
- Debug suppression rule: `added` / `removed` always go to all
  subscribers; `updated` only goes to a non-debug subscriber if the
  *non-debug projection* of the record changed. A frame with empty
  added/updated/removed after filtering is suppressed entirely.
- Bespoke session-list ops are removed (`ListSessionsRequest`,
  `SubscribeSessionListRequest`, `SessionListResponse`,
  `SessionListSnapshotEvent`) along with their `_handle_*` paths.
  `SessionListSubscription` is replaced by sessions appearing in the
  `ResourceSubscription` union as another `resource="sessions"` value.

## What's debug-only today

Per-resource record shape, after 4a stripping:

| resource | non-debug fields | debug-only fields |
|---|---|---|
| `rpc_connections` | `connection_id`, `name`, `peer_addr`, `connected_at` | `service_registration_short_ids`, `entity_registration_short_ids` |
| `services` | `short_id`, `service_type_id`, `external_names` | `registering_connection_ids`, `pinned_by_sessions` |
| `entities` | `short_id`, `fxtr_id`, `external_names` | `registering_connection_ids`, `pinned_by_sessions` |
| `sessions` | `session_id`, `metadata`, `created_at` | (none for now; flag accepted) |

Confirming: drop `last_state_seq` from the sessions record (current
3a snapshot includes it; per the plan, deliberately omitted to keep
sessions resource quiet — that data is on `subscribe_session` /
`subscribe_session_debug`).

## Mutation entry points

Every place that currently calls `notify_subscribers` or
`notify_session_list_subscribers`. Each of these is the boundary for
a single change-buffer flush.

### Top-level entry points (open a fresh buffer)

1. `WorkstationState.add_rpc_connection` — CM enter (rpc_connection
   added) and CM exit (rpc_connection removed; possibly residual
   `release_registration` / `release_entity_registration` from the
   defensive sweep).
2. `WorkstationServiceImpl.register_service` (service_impl.py) —
   adds a Registration; updates services + rpc_connections.
3. `WorkstationServiceImpl.copy_entity` — adds an EntityRegistration;
   updates entities + rpc_connections.
4. `WorkstationServiceImpl.name_connection` — updates a
   rpc_connection's name field.
5. `release_registration` (state.py module-level, called from end-
   callbacks). Currently a leaf entry point.
6. `release_entity_registration` (state.py module-level, called from
   end-callbacks). Currently a leaf entry point.
7. `WorkstationState.create_session` — adds a session.
8. `WorkstationState.apply_mutation` — many things: tracker ops
   churn services/entities, retention prune churns refcounts,
   close drains all session pins → potentially many resource
   updates plus the session removal.
9. Result delivery workers (`deliver_load_result_to_slot`,
   `deliver_invoke_result_to_slot`) — phase 1 attach + phase 3
   commit each touch session refcounts which can flip
   `tracking_sessions` membership on entities/services.
10. `service_record_worker` shutdown path — when the worker drains,
    is there a resource change? Currently, no — `release_registration`
    is what removes the record, and that's a separate entry point.
    The worker just drains the scope. Nothing to do.

### Nested calls (join the parent buffer if active, else open one)

- `release_registration` and `release_entity_registration` are called
  from both end-callbacks (no parent context — they're the entry
  point) and from inside `apply_mutation` (untrack ops, close drain).
  The "join if active, else start" pattern handles both.
- `_incref_session_*` / `_decref_session_*` are always called from
  inside a parent context (`apply_mutation`, delivery workers,
  retention prune).
- `_maybe_release_*_record` is always called from `release_*` or
  `_decref_session_*`, both already inside a context.

## Buffer shape

A `ResourceChangeBuffer` lives on `WorkstationState` (or — see
below — passed via `state._current_buffer`-style attribute). Holds:

```
per_resource: dict[ResourceKind, dict[key, _TouchedRecord]]

class _TouchedRecord:
    pre_image: dict | None   # built on first touch within this txn,
                             # None if record didn't exist pre-txn
    pre_image_captured: bool # so we can distinguish "captured None"
                             # (didn't exist) from "not yet captured"
```

API on the buffer:

- `mark_touched(resource, key)` — captures `pre_image` if not yet
  captured, by reading the current registry. Idempotent within a
  buffer. Called by every primitive that mutates a resource record
  *before* the change happens.
- That's it for the buffer — adds / updates / removals are derived
  at flush time by comparing pre-image to live registry state.

(Alternative considered: explicit `record_added` / `record_updated`
/ `record_removed` calls that pre-classify. Rejected: makes
primitives more invasive and we'd still need pre/post for the
non-debug suppression check. Lazy pre-image capture is simpler.)

### Flush

At flush:

```
for resource, touched in buffer.per_resource.items():
    added, updated, removed = [], [], []
    for key, info in touched.items():
        post = current_record_for(resource, key)  # None if absent
        if info.pre_image is None and post is None:
            continue                              # add+remove cancelled
        if info.pre_image is None:
            added.append(post)
        elif post is None:
            removed.append(key)
        elif info.pre_image == post:
            continue                              # touched but no diff
        else:
            updated.append(post)
    if not (added or updated or removed):
        continue
    for sub in subscribers_of(resource):
        emit_for(sub, added, updated, removed)
```

`emit_for` filters per-subscription:

- `added`: project each record per `sub.debug`, send.
- `removed`: send keys verbatim.
- `updated`: project both pre and post per `sub.debug`; if equal,
  drop (no visible change to this subscriber); else send projected
  post.
- If after filtering all three are empty for this subscriber, skip
  the frame entirely.

## Mutation-context plumbing

Two patterns under consideration:

**(A) Explicit context manager.** Each top-level entry point wraps
its sync body in `with state.mutation_context(): ...`. Nested calls
do `with state.mutation_context(): ...` too, which is a no-op if a
buffer is already active. On the outermost exit, flush.

**(B) Implicit auto-start.** Primitives call `state._touch(resource,
key)`; if no buffer is active they create-and-flush one synchronously
around the change. Multiple touches in the same call chain auto-batch
because each primitive invocation fully completes before the next.

(B) is simpler but loses multi-call batching at the entry-point
level. E.g. `apply_mutation`'s tracker-ops + retention prune would
each flush separately if we used (B) without explicit grouping —
which produces N small frames instead of one batched frame.

Going with (A): explicit `mutation_context()` CM at each top-level
entry point, nested calls become no-ops. Unifies sync and async
entry points (CM works for both; `apply_mutation` is sync, delivery
helpers' phase 1 / phase 3 are sync, end-callbacks are sync).

For the delivery helpers, phase 1 and phase 3 each open their own
mutation context (they're not nested in any larger sync block).
That's correct — phase 1 and phase 3 each commit their own
observable change set.

## Implementation steps (4a)

1. **Wire-protocol changes** in `client_protocol.py`.
   - `ResourceKind` adds `"sessions"`.
   - `SubscribeRequest` gains `debug: bool = False`.
   - Rename `SnapshotEvent` → `ResourceSnapshotEvent`; tag
     `"resource_snapshot"`.
   - New `ResourceChangesEvent`: `subscription_id`, `added`,
     `updated`, `removed`. Tag `"resource_changes"`.
   - Remove `ListSessionsRequest`, `SubscribeSessionListRequest`,
     `SessionListResponse`, `SessionListSnapshotEvent`.
   - Update `ClientRequest` / `ServerFrame` unions accordingly.
   - `parse_text` / `serialize_to_text` unchanged.

2. **`state.py` — buffer + context.**
   - Add `ResourceChangeBuffer` dataclass + `_TouchedRecord`.
   - Add `WorkstationState._current_buffer: ResourceChangeBuffer | None`.
   - Add `WorkstationState.mutation_context()` CM:
     - First entry: allocate buffer, set attribute, yield.
       On exit (success): flush buffer + clear attribute.
       On exit (exception): clear attribute without flushing.
     - Nested entry: yield no-op; on exit do nothing.
   - Add `WorkstationState.touch(resource, key)`. Asserts a buffer
     is active; captures pre-image lazily.
   - `WorkstationState.notify_subscribers(resource)` is removed in
     favor of buffer-driven flush. Ditto
     `notify_session_list_subscribers`.

3. **`state.py` — sessions resource path.**
   - `SessionListSubscription` removed.
   - `ResourceSubscription` accepts `resource="sessions"`. Carries
     `debug: bool` field as well.
   - New `_build_session_record(session)` helper for the sessions
     resource — returns the per-record dict. (Remove
     `_build_sessions_list_snapshot`'s caller-facing form; the
     iteration becomes resource-snapshot machinery.)
   - `build_snapshot("sessions")` covers it.

4. **`state.py` — wire all `notify_subscribers` callsites to the
   buffer.** Touch-points:
   - `add_rpc_connection` (enter): `mutation_context()` around the
     append + `touch("rpc_connections", connection_id)`. Yield. On
     exit (CM finally): another `mutation_context()` around the
     defensive sweep + delete + `touch("rpc_connections", connection_id)`.
   - `release_registration` / `release_entity_registration`: wrap
     bodies in `mutation_context()` (no-op if nested); `touch`
     services/entities + rpc_connections.
   - `_incref_session_*` / `_decref_session_*`: replace
     `notify_subscribers(...)` with `touch(...)` calls. Caller is
     responsible for being inside a context.
   - `_close_session_drain_trackers`: same.
   - `create_session`: wrap body in `mutation_context()`; `touch
     ("sessions", session_id)`.
   - `apply_mutation`: wrap the whole sync body in
     `mutation_context()`. `touch("sessions", id)` for the close-
     branch's session removal. (Internal calls into incref/decref/
     release helpers automatically participate.)

5. **`service_impl.py`** — wrap `register_service`, `copy_entity`,
   `name_connection` in `state.mutation_context()` and replace
   `notify_subscribers` calls (they call into the
   `release_registration` helpers which we've already buffered;
   plus the synthesis side adds via direct registry mutation, so
   `touch` calls go inline).

6. **Subscription wiring (`client_app.py` + state.py).**
   - `_handle_subscribe` accepts the new `debug` field; stores it
     on the `ResourceSubscription`.
   - On subscribe: send a `ResourceSnapshotEvent` (debug-projected).
   - Remove `_handle_list_sessions` / `_handle_subscribe_session_list`.
   - `Unsubscribe` continues to work uniformly.
   - The flush path (called from `mutation_context.__exit__`)
     iterates client connections × subscriptions, applies the
     debug filter, sends frames.

7. **Tests.**
   - `test_client_protocol_unit.py`: new wire shapes, removed
     session-list ops.
   - `test_snapshot_builders.py`: split into
     `test_snapshot_builders.py` (snapshot only — covers the
     non-debug projections too) and `test_resource_deltas.py`
     (delta-driven add/update/remove cases including the
     debug-only-suppression rule).
   - `test_sessions_3a.py`: rewrite list-related tests to use
     `subscribe(resource="sessions")`.
   - `test_sessions_3d.py`: assert `pinned_by_sessions` ticks come
     through to debug subscribers and are suppressed for non-debug.
   - New small end-to-end test on entities: subscribe (debug+non-
     debug), register entity, mutate, unregister, drop connection
     — assert frame sequence on each.

8. Update `CODE_OVERVIEW.md`.

## Order I'd land it in

Smallest reviewable chunks:

1. Protocol changes (`client_protocol.py`) + their unit tests.
2. Buffer + context infrastructure on `WorkstationState`. No
   callsite migration yet; existing `notify_subscribers` keeps
   working.
3. Migrate the per-resource notify callsites to the buffer one
   resource at a time:
   - rpc_connections (smallest set of touchsites).
   - services.
   - entities.
   - sessions (this is also when we light up sessions-as-resource
     and remove the bespoke session-list path).
4. Add the `debug` flag wiring + projection + suppression. Tests.
5. Final test sweep + CODE_OVERVIEW.md.

## Open items

- **Snapshot-on-resubscribe for the new wire shape.** The current
  flow on subscribe is "send snapshot once, then snapshots on
  every change." New flow is "send snapshot once, then
  changes-only." Same single-snapshot-on-subscribe behavior; just
  different follow-up frame type. Confirmed.

- **Order of frames at flush time across resources.** A single
  mutation can touch services + rpc_connections (e.g.
  `release_registration`). Today we send two notify calls in
  whatever order the code calls them. Under the buffer, do we
  emit one frame per resource per subscriber? Yes — frames are
  resource-scoped (subscription_id is resource-specific). Order
  across resources within one subscriber's stream is whatever
  order we iterate; no semantic constraint. Per-subscription
  order across multiple resource subscriptions in a single
  WebSocket is whatever the iteration visits — clients shouldn't
  rely on cross-resource ordering of changes.

- **`SessionEventFrame` broadcast vs. resource-changes broadcast
  ordering when both fire from `apply_mutation`.** A close
  mutation: we emit the terminal `state_update` `SessionEventFrame`
  to foreign session subscribers, *and* emit a `resource_changes`
  with `removed: [session_id]` to sessions-resource subscribers.
  The plan implies "broadcast then teardown." I'd land it as:
  flush the resource-changes buffer *after* session-event broadcast,
  so a resource subscriber that's also subscribed to the session
  observes the close event before it observes the removal. Same
  for the close-time release_registration cascade. I'll lock this
  in 4a; it falls out naturally from the mutation_context's
  `__exit__` running after the apply_mutation body.
