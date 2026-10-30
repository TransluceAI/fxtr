# stage13post1 — design scratch (working notes)

Working notes for the channel-redo design. Not a spec; the spec is `overview.md`
plus the part plans. Captures the reasoning behind the load-bearing decisions so
we can revisit if an assumption breaks.

## What we're shedding (vs the current `streams.py`)

The current system's complexity comes almost entirely from **services-in-messages**:

- A message `V` can carry `ServiceRef` leaves (sub-channels), so retention has to
  *bridge* those refs into the retention scope (`bridge_service_from_scope`,
  `keepalive_via=...`), dedup by `ServiceKey`, and reject refs to services mounted
  on the channel's own/descendant scope (the deadlock footgun + the unimplemented
  "drain-started signal" core extension).
- `streaming_scheduler` had to mount per-cell channels on a *separate* `cells_scope`
  to dodge that footgun, then bridge the cell sink into the dispatch call scope.
- The reader had to retain bridged service leaves into the consumer scope inside the
  delivery dispatch (`iter_channel`'s `_IterSubscriberImpl`).

The redo bans services from messages entirely. A message/status/final is **pure
data** (entity refs + scalars). So retention is just "copy entity closures into a
scope" — no bridging, no `ServiceKey` dedup, no own-scope rejection, no core
extension. That is the single biggest simplification.

## Tree model

One in-process **`_ChannelTree`** object, created by `create_root_channel`. It owns:

- `mount_scope`: the scope the root reader+writer were mounted on (passed to
  `create_root_channel`, default active). Long-lived = tree lifetime; it's where the
  root node's `durable_scope`/`data_scope` holders + every node's hubs run, and it
  bounds the tree. (Per-node tag/final/log/status residency lives in the node's own
  `durable_scope`/`data_scope`, not here — see residency tiers. Thin impls mount on
  the per-call child scope, not here — see below.)
- `nodes: dict[ChannelId, _ChannelNode]` — the registry. `ChannelId` is an internal
  opaque id (not the user-facing subchannel key; see "keys" below).
- the tree's task group (for retention-holder tasks + the per-node hubs), opened by
  the root mount's lifespan.

A **`_ChannelNode`** is the actual per-channel state (the only stateful thing —
impls are thin routers bound to `(tree, node_id)`):

```
key: str | None              # user-facing subchannel key (None for the root)
name: str | None             # debug label (prefix of key)
parent: ChannelId | None
children: dict[str, ChannelId]   # key -> child node  (per-parent namespace)
started: anyio.Event         # set by writer.start(); get_tag/peek await it
stage: "open" | "complete" | "abort"
tag_ref / tag_type / s_type / v_type / r_type   # types from create_root_channel's tag_type or start_channel_and_infer_types
status_encoded: FxtrServiceData          # latest status (replace-only)
log: list[FxtrServiceData]               # encoded V batch contents, in order
final_encoded / abort_reason
full_hub: SubscriberHub[FxtrServiceData]   # mode="queue"  (subscribe_full)
state_hub: SubscriberHub[FxtrServiceData]  # mode="coalesce" (subscribe_state)
durable_scope: FxtrScope     # per-node: tag + final residency (lifetime = while PARENT open)
data_scope: FxtrScope        # per-node: current log + status residency (transient)
data_release: anyio.Event    # data_scope holder-task release, swapped on replace_history
lock: anyio.Lock
```

**Lifetime rule (settled):** a node is **reachable iff its parent is open** —
`open_subchannel_reader` on a finished/aborted node raises, and a node's resources
(both scopes + both hubs + bookkeeping) live only while its parent is open. The
**parent's final value is the complete record of its children**, so nothing is lost
by making children inaccessible after the parent finishes. The root has no parent,
so its lifetime is the tree (`create_root_channel`'s scope).

Thin impls (cheap, stateless except the `(tree, node_id)` binding):
- `_WriterImpl(TypedServiceImpl[FxtrChannelWriter[...]])` bound to `(tree, node_id)`.
- `_ReaderImpl(TypedServiceImpl[FxtrChannelReader[...]])` bound to `(tree, node_id)`.
- Both route every method to `_ChannelNode` / `_ChannelTree` helpers (mirrors how
  `_SinkImpl`/`_StorageImpl` route to `_ChannelState` today).

### Impls mount on the per-call child scope; the tree owns the durable state

A service method runs in a per-call child scope, and you *can* mount a sub-service
on it and return its ref — the standard service-return path bridges it to the caller
(`concurrent_child_bridge`, the RPC service-return shape; see
`notes/scope_service_lifetimes.md`). So `spawn_subchannel` /
`open_subchannel_reader` mount the new thin impl on the call's active (per-call
child) scope (Daniel's "mount inside the call") and return it; the **caller's hold
governs the impl's lifetime**. The durable per-node state (hubs, log, data scope,
bookkeeping) lives on the tree, independent of any particular impl — so an impl
tearing down loses no data.

### Channel lifetime = bookkeeping + writer-release abort + parent-teardown walk

A node is **open** until one of:
1. an explicit `finish` / `abort` on it, or
2. its **parent** finishes/aborts, or
3. its **writer impl's mount scope drains** — the writer carries a
   `_fxtr_mount_lifespan` whose teardown calls `abort_if_open(node)` (the
   release-without-finish safety net; idempotent), or
4. the tree closes — the root writer is mounted on `mount_scope`, so its
   teardown-abort fires on tree close and cascades to all open children.

When a node finishes/aborts, an **explicit teardown walk** reclaims its children
(*not* a structural `aclose` cascade — a non-forced parent `aclose` would wait
gracefully on its children, the wrong behavior here; we want active control). The
walk, per child: abort it if still open (publish `ChannelAbortedEvent` to its full
hub so live subscribers get a clean signal — then recurse into *its* children),
`aclose` its hubs, release its `data_scope` + `durable_scope`, and drop it from the
registry. After a node finishes/aborts, `open_subchannel_reader(key)` on it raises.

The reader impl's teardown is a **no-op** (reading never affects channel lifetime;
subscriptions are independently governed by their `UnsubscribeHandle`s via the hub).

Ownership convention (the scheduler→task case): the **producer that holds the writer
owns the channel** — it `start`s, `emit`s, and ideally `finish`es it. A coordinator
that spawned the subchannel on the producer's behalf (the scheduler) does *not*
start/finish it; after the producer is done it may call `abort` (idempotent) as a
deterministic backstop, then emit its own result event. `abort_if_open` on
writer-release is the last-resort net under all of this.

## Residency tiers

| data | scope | lifetime |
|------|-------|----------|
| tag entity closure | `node.durable_scope` | while parent open |
| final value `R` closure | `node.durable_scope` | while parent open |
| message log `V` closures | `node.data_scope` | until finish/abort/replace_history |
| latest status `S` closure | `node.data_scope` | until replace_history swap / finish |

Both are isolated scopes (`include=[GLOBAL_SCOPE]`, isolated) held open by holder
tasks on the tree task group — the proven `_retention_holder` pattern, two per node.
`durable_scope` holds the tag (copied in at `start`) + the final (copied in at
`finish`); it's released by the parent-teardown walk (or tree close for the root).
`data_scope` holds the current log + status; `replace_history=True` opens a fresh
one, copies the new history + current status in, swaps, releases the old (the current
`replace_snapshot` swap); `finish`/`abort` release it (the final already lives in
`durable_scope`). Per-node isolated scopes (vs one shared scope) give **atomic
release with no per-id `abandon`** — content-addressed ids can be shared across
messages/channels, so per-id release in a shared scope would risk breaking a payload
that still needs the id.

### Reader returns without ancestry

`copy_from_scope(id, other_scope)` takes the source scope **explicitly** and walks
the DAG — it does not require `other_scope` to be an ancestor. So a reader impl
returns tag/final by copying from `node.durable_scope`, and log/status from
`node.data_scope`, into its dispatch call scope before returning. Hub deliveries
(`subscribe_*`) use the matching scope as `origin_scope`, so the hub captures a
per-event lease and copies during delivery — works regardless of where the impl is
mounted.

## Keys & the pre-start window

- `spawn_subchannel(name)` mints a fresh unique `key` (e.g. `f"{name}-{uuid4().hex}"`
  if `name` else `uuid4().hex`) — name is a debug prefix only. Registers a child node
  (stage open, `started` unset, its `durable_scope`/`data_scope`/hubs created under
  the parent for reclaim) under the parent, mounts a `_WriterImpl` for it on the
  per-call active scope, returns `(key, writer_ref)`.
- `open_subchannel_reader(key)` raises if this node is finished/aborted; else looks up
  `parent.children[key]` (per-parent namespace; unknown key → `IRException`), mounts a
  `_ReaderImpl` on the per-call active scope, returns the bridged ref. The *user*
  walks the tree (no auto-walk).
- Pre-start window (spawned, key emitted, reader opened, but `start()` not yet
  called): `subscribe_full`/`subscribe_state` succeed immediately but deliver nothing
  until `start` fires (the hub simply has no seed yet — we seed on `start`, or we
  seed lazily: subscribe holds, and `start` publishes the open snapshot to all
  current subscribers). `get_tag`/`peek_full`/`peek_state` **block on
  `node.started`** (also woken by abort → they then return/observe the aborted
  state; `get_tag` on an aborted-before-start channel raises).

Decision: seed-on-start. `subscribe_*` before start registers with no seed; the
`start` path publishes the initial open-snapshot (full) + initial state to the hubs,
so both already-waiting and future subscribers converge. After start, `subscribe_*`
seeds normally from current state under the lock.

## Status (S)

- `ChannelState[S] = {status: S, stage}` — `subscribe_state` (coalesce hub) + the
  cheap `peek_state`.
- `subscribe_full` subscribers ALSO see status: every snapshot carries current
  status; a status-only `emit(status=X, events=None)` produces a full update event
  with empty `events` + new status (decision 9/10 — push the whole delta, let
  subscribers ignore fields they don't care about).
- `S=None` is allowed (migrated schedulers start there). Later (part5) we add a real
  job-status payload + a frontend badge driven by `subscribe_state`.

## Event/wire shape (batched)

One `emit` call → one `ChannelUpdateEvent` carrying the **batch** `events: list[V]`,
the current `status`, and the `replace_history: bool` flag. Snapshots carry status
too. **No event carries the tag** — it's fetched once via `get_tag()`, so the whole
family is `T`-free (mirrors `ChannelState`). Renamed dataclasses (stream→channel,
+S, −tag):

```
OpenChannelSnapshot[S,V]        {status, values, kind="snapshot_open"}
CompleteChannelSnapshot[S,R]    {status, result, kind="snapshot_complete"}
AbortedChannelSnapshot[S]       {status, reason, kind="snapshot_abort"}
ChannelUpdateEvent[S,V]         {events: tuple[V,...], status, replace_history, kind="update"}
ChannelCompleteEvent[S,R]       {result, status, kind="complete"}
ChannelAbortedEvent[S]          {status, reason, kind="abort"}
type ChannelUpdate[S,V,R] = (union)
```

Every event carries the (possibly unchanged) current status value, so a subscriber's
fold keeps a single "latest status" without tracking deltas. (No separate "unchanged"
sentinel on the wire; the writer re-sends the current status when `emit` didn't set
one.) Terminal events (`complete`/`abort`) read their status from `durable_scope`
(where the latest status is mirrored at finish/abort), since `data_scope` is released
then.

## Capability split

Consumers get **reader** refs only; producers get **writer** refs only. Schedulers
announce the run **reader**; the caller-passes-sink pattern becomes
caller-passes-**writer** (a spawned subchannel's writer ref handed to a task as a
method arg — an arg, not a message, so it's allowed).

## Open / revisit
- Status residency: `S` sits in `data_scope` with the log, so old status closures
  linger until the next `replace_history`/`finish`. Fine when `S` is scalar/`None`
  (the common case); if `S` carries refs and updates often, add a dedicated 1-element
  swapped status scope.
- part5 status payload shape (running/idle/queued/...): design with the frontend
  badge.
