# Stage 13post1: Output-channel redo — overview

A ground-up rework of the output-channel system (`fxtr2.common.streams`,
stage 13 part 1) that removes the lifetime complexity around sending channels
through channels, and cleanly separates the reader and writer ends. Motivation
and the target surface are in
[sketch_part5_simplification.md](sketch_part5_simplification.md); the design
reasoning is in [overview_scratch.md](overview_scratch.md).

The end state replaces **all** users of the old system (the two demo schedulers,
their task libs, the frontend channel package, the demo renderers, the tests, the
nix type-id stub) — by the end of the stage the old `streams.py` is gone and
everything is on the new API. Intermediate parts may leave consumers temporarily
broken if that simplifies the cut-over (core first, then users).

## What changes, in one paragraph

A channel is no longer one object exposing a write-`sink` + read-`storage` pair.
It is a **tree of channels** backed by one in-process state object created at the
root, fronted by two distinct services per node: a **writer**
(`FxtrChannelWriter` — `start`/`emit`/`finish`/`spawn_subchannel`/`abort`) handed to
**producers**, and a **reader** (`FxtrChannelReader` —
`get_tag`/`subscribe_state`/`subscribe_full`/`peek_state`/`peek_full`/
`open_subchannel_reader`) handed to **consumers**. Channel **messages carry only
data** (entity refs + scalars), never service refs — to expose a sub-computation
you `spawn_subchannel` (getting a fresh writer + a string **key**) and emit the
*key*; a consumer recognizes the key by app convention and calls
`open_subchannel_reader(key)`. Channels gain a lightweight **status** dimension
(`S`) that is replace-only and separately subscribable. The `stream`→`channel`
rename is applied throughout.

## Doc map

- [part1_core.md](part1_core.md) — `fxtr2/common/channels.py` (replaces
  `streams.py`): `ChannelTag`, the status/event/snapshot dataclasses, the two
  service protocols, the channel **tree + node + thin impls**, the
  `create_root_channel` / `start_channel_and_infer_types` /
  `infer_channel_reader_type` helpers, `decode_channel_update` / `iter_channel`,
  the `nix/fxtr_lib_type_ids` stub, and tests. The bulk of the work.
- [part2_frontend.md](part2_frontend.md) — `@fxtr-ui/rd-types-channel`, redesigned
  around rendering **any** channel from its reader (no consumer-schema knowledge):
  the reusable `ChannelReaderView` (reader → `get_tag` → tag-type dispatch), the
  service-type renderers keyed on `CHANNEL_READER_TYPE_ID`, the `subscribe_full` fold
  (batched-update + `replace_history`), **live `subscribe_state` previews** for the
  compact keys, `SubchannelView`/`useSubchannelReader` (key → `open_subchannel_reader`
  → child reader, recursive), the generic fallback card driven by
  `subscribe_debug_subchannels`, the rename, and tests. Depends on the **1e** core
  feed below.
- **Core 1e (`subscribe_debug_subchannels`)** — a small core follow-on (see
  [part1_core.md](part1_core.md) "Debug subchannel feed"), done before part 2's
  fallback work: a reader-side replay feed of `SubchannelOpened{key, name}` so the
  generic card can enumerate subchannels without app-schema knowledge.
- [part3_simple.md](part3_simple.md) — migrate `simple_scheduler` + `simple_tasks`:
  one root channel per run (no subchannels), scheduler owns + finishes it,
  announces the **reader**, `S=None`. Its grid renderer + tests.
- [part4_streaming.md](part4_streaming.md) — migrate `streaming_scheduler` +
  `streaming_tasks` + the task libs: per-cell **subchannels** via
  `spawn_subchannel`, scheduler passes the cell **writer** to the task (task
  `start`/`emit`/`finish`es it), scheduler aborts as backstop + emits a
  `cell_result`; the cache short-circuit (part4post1) emits no subchannel; frontend
  drills into cells via `open_subchannel_reader(key)`. Tests.
- [part5_status_badge.md](part5_status_badge.md) — (cleanup, **shrunk**) give the
  schedulers a real job-status payload (running/idle/…) on the status channel and show
  it in the badge. The `subscribe_state` *plumbing* (hook + fold + live previews) is
  built in part 2; part 5 is now just the backend payload + the visual.

## Conceptual model

A **channel node** = a **tag** (`EntityRef[T]` whose type identifies the kind of
stream) + a **status** (`S`, replace-only) + a sequence of **messages** (`V`) + a
**final value** (`R`) that replaces the messages on finish. Nodes form a tree:
each writer can `spawn_subchannel` to mint a child (keyed by a fresh string), each
reader can `open_subchannel_reader(key)` to reach a direct child. Subscribers to a
node's **full** stream get the message log as a snapshot then live batched deltas
(each carrying current status); subscribers to its **state** stream get only the
replace-only `ChannelState` (status + lifecycle stage). A finished/aborted node
serves only its final value / abort reason to new subscribers.

## Key decisions (settled with Daniel)

1. **Reader/writer split, two fixed service type ids.** One id for every writer,
   one for every reader, in the existing `FXTR_STREAMS_NS` UUID (kept — decision 12).
   The frontend recognizes "this is a channel" by the **reader** type id; per-stream
   renderer dispatch is one level up, on the tag entity's type (unchanged model).
   Consumers only ever receive reader refs; producers only writer refs.

2. **Data-only messages; subchannels by key.** `V` / `S` / `R` must serialize to
   `FxtrChannelData` *minus its `ServiceRef` arm* — no service refs in any channel
   payload. Sub-computations are exposed as `spawn_subchannel` → emit the returned
   key; consumers `open_subchannel_reader(key)`. This deletes the entire
   retention-bridging / own-scope-rejection / `cells_scope` machinery.

3. **One in-process tree state; cheap throwaway impls.** `create_root_channel`
   builds a `_ChannelTree` on its mount scope holding all per-node state (stage,
   parent/children registry, two `SubscriberHub`s, message log, status, residency
   scopes). Writer/reader impls are thin routers mounted on the per-call child scope
   and returned via the normal service-return bridge; the caller's hold governs the
   impl lifetime, never the data.

4. **Lifetime = bookkeeping + writer-release abort + reachable-iff-parent-open.** A
   node is open until explicit `finish`/`abort`, parent finish/abort, **writer-impl
   teardown** (`_fxtr_mount_lifespan` → `abort_if_open`, the release-without-finish
   net), or tree close. Reader teardown is a no-op. The producer holding the writer
   owns the channel and ideally `finish`es it; a coordinator may `abort` (idempotent)
   as a backstop. A node is **reachable iff its parent is open**: each node owns a
   `durable_scope` (tag + final) and a `data_scope` (log + status), and when a node
   finishes/aborts an **explicit teardown walk** reclaims its children's scopes +
   hubs + bookkeeping (not a structural `aclose` cascade, which would block on
   children) — so `open_subchannel_reader` on a finished node raises. The parent's
   final value is the complete record of its children, so nothing is lost. This
   bounds memory to the tree's open frontier and removes any shared-scope per-id
   release hazard.

5. **Status dimension.** `ChannelState[S] = {status, stage}`. `subscribe_state`
   (coalesce hub) + `peek_state` are the cheap replace-only view; `subscribe_full`
   subscribers *also* see status (snapshots carry it; a status-only `emit` is a full
   update with empty `events` + new status). `S=None` is allowed (migrated schedulers
   start there); part5 adds a real job-status payload + badge.

6. **Batched updates + `replace_history`.** One `emit(events=[...], status=...,
   replace_history=...)` call surfaces as one `ChannelUpdateEvent` carrying the batch
   `events: tuple[V, ...]`, the current status, and the `replace_history` flag —
   pushed to **all live** full subscribers (decision 10), who may act on or ignore
   the flag. `replace_history=True` also resets the new-subscriber snapshot to the
   batch.

7. **Pre-start window.** A writer is created unstarted (by `spawn_subchannel` or
   `create_root_channel`); `start(tag, initial_status)` runs once. Before start:
   `subscribe_*` succeed but deliver nothing (seeded on `start`);
   `get_tag`/`peek_*` block until started (woken by abort → observe/raise the aborted
   state). `emit`/`finish` before `start`, and double-`start`/`finish`, raise.

8. **Keys.** `spawn_subchannel(name=None)` mints a fresh unique key (`name` is a
   debug prefix only) and registers a child under the **per-parent** key namespace;
   the user walks the tree explicitly (no auto-walk). `open_subchannel_reader`
   resolves only direct children of that reader's node.

## Sequencing

```
part1 (fxtr2 core: channels.py + tree + helpers + tests)  [1a–1d done]
   │
   └─► core 1e (subscribe_debug_subchannels feed)
          │
          ├─► part2 (frontend pkg: ChannelReaderView, subscribe_full fold,
          │          live subscribe_state previews, SubchannelView, debug fallback)
          │        │
          ├─► part3 (simple_scheduler/simple_tasks + renderer + tests) ──┐
          │                                                              ├─► (live demo)
          └─► part4 (streaming_* + task libs + renderer + tests) ────────┘
                                                                          │
                                                       part5 (status payload + badge)
```

Part 1 is fully testable with pytest in `v2/`. Part 2 depends only on part 1's wire
shapes (vitest + mocks). Parts 3 and 4 wire it into the live demo (part 4 is the
subchannel exercise + the existing output cache). Part 5 is the status-badge
cleanup. Old `streams.py` is removed in part 1 (its tests rewritten); demo consumers
are temporarily broken between part 1 and parts 3–4, which is acceptable.

Each part is implemented in **chunks with `jj pre` + a commit per chunk**; part 1's
chunking (3 chunks, review pauses after 1a and 1b) is firm and detailed in
[part1_core.md](part1_core.md), and parts 2–5 carry *tentative* chunking notes to
revisit when we reach them.

## Open questions / decisions to review

Judgment calls and deviations made while planning — to discuss before/while
implementing. Grouped by how load-bearing they are.

**Confirmed with Daniel (recorded for the record):**
- Impls mount on the per-call child scope and ride the normal service-return bridge;
  the **writer impl's `_fxtr_mount_lifespan` teardown aborts-if-open** (release net).
  Reader teardown is a no-op. (decision 3/4)
- Task owns its subchannel writer (starts/emits/finishes); the coordinator
  (scheduler) `abort`s as an idempotent backstop and emits its own result event.
  (part 4)
- Batched updates; `replace_history` pushed live to all full subscribers with the
  flag; status-only `emit` is an empty-`events` full update; full subscribers also
  see status. (decisions 5/6)
- Keys are freshly minted with `name` as a debug prefix; per-parent namespace; user
  walks the tree explicitly. (decision 8)
- Keep `FXTR_STREAMS_NS` UUID; in-place all-at-once cut-over (intermediate breakage
  OK). (decision 12)
- `subscribe_full`/`subscribe_state` are plain named methods (no `EventSource`
  subclassing); the frontend selects via the `subscribe_to(source, method)` arg,
  which the server + connectors already forward. (part 1)
- **Frontend renders any channel from its reader (part 2, settled).** The task starts
  the channel, so a producer that spawns a subchannel does **not** know its tag — a
  channel message carries only the **key**, and the tag comes solely from `get_tag()`
  on the opened reader. So the frontend is built around one reusable unit
  `ChannelReaderView` (reader → `get_tag` → dispatch the tag-type card) used by both
  the service-type `FullPanel` and by `SubchannelView` (open child by key → render it
  the same way). The package never interprets message contents; key conventions live
  in app-specific cards. The full view needs only `get_tag` (the card subscribes for
  the rest); `peek_state` is unused on the frontend (kept in the core for non-UI
  consumers).
- **Live `subscribe_state` previews now (part 2), part 5 shrinks (settled).** The
  compact link/preview keys subscribe to `subscribe_state` (lifecycle-only, no bulky
  events) via their dispatcher slot, so a preview pill is live. `subscribe_state` is
  the primary lifecycle signal in previews; `get_tag` is best-effort for the tag (rich
  per-type dispatch + reflink), handling the pending ("starting…") and
  aborted-before-start cases from the state stream. Part 5 keeps only the backend
  status *payload* + the visual.
- **Debug subchannel feed (core 1e, settled).** A reader-side
  `subscribe_debug_subchannels` replay feed of `SubchannelOpened{key, name}` (minimal —
  no per-child status; the link picks that up itself). The generic fallback card lists
  each as a **lazily-opened child reader rendered through `RefLink`** — which routes
  through the compact preview keys, so each entry shows a live state pill and is
  clickable to open the heavy `subscribe_full` view. "Somewhat lazy": each child is
  opened + state-subscribed (cheap); full events are pulled only on open. (Cost note:
  a wide fan holds N child readers + N state subs — fine at demo scale.)
- **Residency + reclaim (decision 4, settled):** per-node `durable_scope` (tag +
  final) + per-node `data_scope` (log + status, swapped on `replace_history`); a node
  is reachable iff its parent is open; `finish`/`abort` runs an explicit teardown walk
  reclaiming children's scopes/hubs/bookkeeping; `open_subchannel_reader` on a
  finished node raises. Contract: **a parent's final value must carry any child info
  consumers need post-finish.** (Replaces the earlier "data tiers" / "never reclaim"
  open questions.)
- **Abort-before-start (settled):** an after-start abort keeps its last status
  (`ChannelAbortedEvent`); a never-started abort is the distinct, statusless
  `ChannelAbortedBeforeStart` variant, delivered on both the full and state streams
  (`ChannelStateEvent = ChannelState | ChannelAbortedBeforeStart`) so `peek_*` /
  `subscribe_*` always return a concrete value rather than blocking/raising. `get_tag`
  still raises for that case (no tag). `ChannelState` renamed `stage`→`kind` so that
  field is the state-union discriminator; `status` stays non-optional.

**Judgment calls I made — want a sanity check:**
1. **Full update always carries the current status value** (no "unchanged" sentinel
   on the wire) — so a subscriber fold keeps one latest-status field. (part 1)
2. **Type-id naming**: keep the namespace UUID but rename the types
   `channel_storage`→`channel_reader` (+ new `channel_writer`), which *changes the
   id-string* the frontend dispatch key uses (preset + `fxtr_lib_type_ids` updated).
   Alternative: reuse `channel_storage`'s string for the reader to avoid frontend key
   churn. (parts 1/2)
3. **Scheduler stays a plain `EventSource`** announcing the run *reader*, rather than
   becoming a root channel whose runs are subchannels. The latter is a tempting
   further simplification but a bigger change — deferred. (part 3)

**Smaller / settle-at-impl-time:**
- Whether cell channels (not just run channels) get a real status in part 5, and the
  exact status vocabulary. (part 5)
- Status residency: `S` shares `data_scope` with the log, so old status closures
  linger until the next `replace_history`/`finish` — fine for scalar/`None` status;
  add a dedicated swapped status scope only if `S` carries refs and updates often.
- The `origin_scope` for state-hub publishes when `S` carries entity refs (data
  scope) vs not (`None`). (part 1)

## Out of scope
- Provenance-aware channels over the wire (tracked elsewhere, stage 9).
- Disk-durable / content-addressed channel persistence.
