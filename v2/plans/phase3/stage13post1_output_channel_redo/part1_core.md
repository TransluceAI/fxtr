# Part 1: Core — `fxtr2/common/channels.py`

Replace `fxtr2/common/streams.py` with `fxtr2/common/channels.py` (and rename the
test module). The old `streams.py` is deleted in this part; its consumers (the demo
schedulers) break until parts 3–4. Builds on the same `fxtr2.services` surface as
before: `SubscriberHub`, `Subscriber`/`UnsubscribeHandle`, `@service_protocol` /
`TypedServiceImpl`, and the dialect codecs (`encode_to_service_data` /
`decode_from_service_data`). No `fxtr2` core (entity/scope/lease) changes are needed
— banning service refs from messages removes the only reason the old design wanted a
"drain-started signal" core extension.

See [overview_scratch.md](overview_scratch.md) for the tree-state / residency-tier /
impl-lifetime reasoning this part implements.

## Chunking & review pauses

Implemented in 3 chunks with a review pause after each of the first two (each chunk
gets its own `jj pre` + commit):

- **1a — surface + smoke gate.** Type ids, `FxtrChannelData`, `ChannelTag` +
  `infer_channel_types`, the dataclasses, the two protocols, the `fxtr_lib_type_ids`
  stub; delete `streams.py`; test-module skeleton; run the smoke gate. *Pause* —
  confirms the only thing that could force a fallback design, and pins the wire /
  protocol surface that parts 2–4 depend on.
- **1b — the engine.** Tree / node / both thin impls / all method behaviors / per-node
  scopes + holders / teardown walk / helpers, plus 2–3 inline smoke tests only (not
  the full suite). *Pause* — this is where the design subtleties land (per-call mount
  + service-return bridge, teardown walk, hub origins, status-mirror-to-durable);
  most likely point to sync before committing to the full surface. (May sub-pause at
  "single channel works, subchannels next" if it gets hairy.)
- **1c — full suite + docs.** The 12-test plan + CODE_OVERVIEW / tests overview;
  `v2/` suite green.

## Namespace + type ids

Keep `FXTR_STREAMS_NS` (same UUID — decision 12). Define **two** fixed ids:

```python
CHANNEL_WRITER_TYPE_ID = system_entities.define_type(FXTR_STREAMS_NS, "channel_writer")
CHANNEL_READER_TYPE_ID = system_entities.define_type(FXTR_STREAMS_NS, "channel_reader")
```

(The old `CHANNEL_STORAGE_TYPE_ID` / `CHANNEL_SINK_TYPE_ID` are removed; the frontend
keys on `CHANNEL_READER_TYPE_ID` now.) Update `nix/fxtr_lib_type_ids` to re-export
`CHANNEL_READER_TYPE_ID` (the value the frontend preset's `lib.entities` needs);
keep the writer id un-exported unless a frontend use appears.

## `FxtrChannelData` (payload vocabulary)

Same doc-only alias as today **minus the `ServiceRef` arm** — channel payloads
(`V`/`S`/`R`) are pure data. Document that sub-channels are referenced by key
(a `str`), not by embedding a service ref. The `R`-must-not-contain-service-refs
note becomes "no channel payload may contain a service ref" (enforced opportunistically
at encode — see emit/finish).

## Tag base + type inference

```python
class ChannelTag[S, V, R](Entity):
    """Abstract base for channel-tag entity types; pins status S, message V,
    and final-value R so a started channel's types can be inferred from its tag."""
```

- Concrete tags: `class MyTag(DataclassEntity, ChannelTag[S, V, R], fxtr_type=...)`.
- `infer_channel_types(tag_type) -> tuple[S, V, R]` — the renamed/extended
  `infer_stream_types`: a `functools.cache`d `types.get_original_bases` walk finding
  the `ChannelTag[...]` specialization, now returning a 3-tuple. Same error rules
  (non-tag / unparameterized / still-generic → `TypeError`, pass explicit types).

## Status / event / snapshot dataclasses

```python
@dataclass(frozen=True)
class ChannelState[S]:                       # state stream, started channel
    status: S
    kind: Literal["open", "complete", "abort"]   # `kind` doubles as the union discriminator

@dataclass(frozen=True)
class ChannelAbortedBeforeStart:             # both streams: aborted before ever starting
    reason: str; kind = "aborted_before_start"   # no tag, no status

@dataclass(frozen=True)
class OpenChannelSnapshot[S, V]:
    status: S; values: tuple[V, ...]; kind = "snapshot_open"
@dataclass(frozen=True)
class CompleteChannelSnapshot[S, R]:
    status: S; result: R; kind = "snapshot_complete"
@dataclass(frozen=True)
class AbortedChannelSnapshot[S]:
    status: S; reason: str; kind = "snapshot_abort"
@dataclass(frozen=True)
class ChannelUpdateEvent[S, V]:
    events: tuple[V, ...]; status: S; replace_history: bool; kind = "update"
@dataclass(frozen=True)
class ChannelCompleteEvent[S, R]:
    result: R; status: S; kind = "complete"
@dataclass(frozen=True)
class ChannelAbortedEvent[S]:                # aborted *after* start — keeps last status
    status: S; reason: str; kind = "abort"

type ChannelUpdate[S, V, R] = (OpenChannelSnapshot | CompleteChannelSnapshot
    | AbortedChannelSnapshot | ChannelUpdateEvent | ChannelCompleteEvent
    | ChannelAbortedEvent | ChannelAbortedBeforeStart)
type ChannelStateEvent[S] = ChannelState[S] | ChannelAbortedBeforeStart
```

- `subscribe_full` / `peek_full` deliver `ChannelUpdate[S, V, R]`; `subscribe_state` /
  `peek_state` deliver `ChannelStateEvent[S]`.
- **Abort split.** A channel aborted *after* starting has a valid last status, so its
  abort keeps it (`ChannelAbortedEvent` / `AbortedChannelSnapshot`). A channel aborted
  *before* ever starting has no tag/status — represented by `ChannelAbortedBeforeStart`
  (just a `reason`), delivered on both streams so `peek_*`/`subscribe_*` always return
  a concrete value (no block/raise). `get_tag` still raises for that case (no tag).
- **No event carries the tag** — it's a channel property, fetched once via
  `get_tag()` (so `ChannelUpdate` is `T`-free, mirroring `ChannelState`). The reader
  protocol keeps `T` solely for `get_tag() -> EntityRef[T]`.
- Every full event carries the **current status** (so a fold keeps one latest-status
  field, no delta tracking). Snapshots carry it too. (The terminal events'
  status is read from `durable_scope` — see finish/abort.)
- Wire form = the mashumaro encoding of these dataclasses as `FxtrServiceData` dicts,
  as today. Manual seed envelopes pinned byte-equal to the dataclass encoding by test.

## Service protocols

```python
@service_protocol
class FxtrChannelWriter[T: Entity, S, V, R](Protocol):
    async def start(self, tag: EntityRef[T], initial_status: S) -> None: ...
    async def emit(self, *, events: list[V] | None = None,
                   status: S | _UseRemoteDefault = USE_REMOTE_DEFAULT,
                   replace_history: bool = False) -> None: ...
    async def finish(self, final_value: R,
                     final_status: S | _UseRemoteDefault = USE_REMOTE_DEFAULT) -> None: ...
    async def spawn_subchannel(self, name: str | None = None
        ) -> tuple[str, ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]]: ...
    async def abort(self, reason: str) -> None: ...

@service_protocol
class FxtrChannelReader[T: Entity, S, V, R](Protocol):
    async def get_tag(self) -> EntityRef[T]: ...
    async def subscribe_full(self, subscriber: ServiceRef[Subscriber[ChannelUpdate[S, V, R]]]
        ) -> ServiceRef[UnsubscribeHandle]: ...
    async def subscribe_state(self, subscriber: ServiceRef[Subscriber[ChannelState[S]]]
        ) -> ServiceRef[UnsubscribeHandle]: ...
    async def peek_full(self) -> ChannelUpdate[S, V, R]: ...
    async def peek_state(self) -> ChannelState[S]: ...
    async def open_subchannel_reader(self, key: str
        ) -> ServiceRef[FxtrChannelReader[_U, _U, _U, _U]]: ...
    async def subscribe_debug_subchannels(
        self, subscriber: ServiceRef[Subscriber[SubchannelOpened]]
        ) -> ServiceRef[UnsubscribeHandle]: ...   # see "Debug subchannel feed" (chunk 1e)
```

`_U = service_dialect.OverrideThisTypeBeforeUsing` and `_UseRemoteDefault` /
`USE_REMOTE_DEFAULT` are the existing typed-service sentinels.

`subscribe_full` / `subscribe_state` are **plain named methods** (no `EventSource`
subclassing). The frontend reaches either by passing the **method name** as the
second arg of the inbox's `subscribe_to(source, method)` invoke — the workstation
inbox (`subscribe_to(event_source, method="subscribe")`) and the connectors already
forward `args[1]` as the subscribe method (so `subscribe_full` / `subscribe_state`
work with no workstation change; the frontend just supplies the name — part 2). Each
method retypes the subscriber ref to `Subscriber[FxtrServiceData]` internally and
hands it to the matching hub (full hub vs state hub), as the old
`_StorageImpl.subscribe` did; wire dispatch is by method name.

## Tree, node, thin impls

`_ChannelTree` (created by `create_root_channel`, owns `mount_scope`, the node
registry, and the lifespan task group) and `_ChannelNode` (per-channel durable
state) are specified in [overview_scratch.md](overview_scratch.md) ("Tree model" /
"Residency tiers"). Key points for implementation:

- The tree's lifespan (attached to `mount_scope`) opens the task group hosting every
  node's hubs + retention-holder tasks, and on teardown aborts the root node — which
  runs the teardown walk over the whole tree.
- **Lifetime rule:** a node is reachable iff its parent is open. Each node owns a
  `durable_scope` (tag + final) and a `data_scope` (current log + status), both
  released when the node's parent finishes/aborts (or tree close, for the root). The
  parent's final value is the complete record of its children, so post-parent-finish
  child access is intentionally gone.
- **Teardown walk** (explicit, *not* a structural `aclose` cascade — a non-forced
  parent `aclose` would block on its children): when a node finishes/aborts, for each
  child: abort it if still open (publish `ChannelAbortedEvent` to its full hub, then
  recurse into its children), `aclose` its hubs, release its `data_scope` +
  `durable_scope`, drop it from the registry.
- `_WriterImpl` / `_ReaderImpl` are `TypedServiceImpl`s bound to `(tree, node_id)`,
  routing each method to node/tree helpers. `_own_full_type_for_serialization`
  returns the parameterized impl type from the node's `(tag_type, s, v, r)` (the
  generic-impl pattern, as `_SinkImpl`/`_StorageImpl` do today).
- `_WriterImpl` carries a `_fxtr_mount_lifespan` whose **teardown calls
  `tree.abort_if_open(node_id, reason="writer released")`** (the release net). The
  reader impl has the default no-op lifespan.
- `spawn_subchannel` / `open_subchannel_reader` mount the new impl on the per-call
  active scope (`await get_active_scope().mount(...)`) and return its ref — the
  service-return path bridges it to the caller (caller-gated lifetime).

### Method behaviors (all node-mutating paths under `node.lock`)

- **`start(tag, initial_status)`** — once only (double → `IRException`). Copy the tag
  closure into `node.durable_scope`; set `tag_ref`/types/`status`; set `node.started`. Seed
  both hubs for already-waiting subscribers by publishing the initial open snapshot
  (full) + `ChannelState(status, "open")` (state). Types: if the node was created
  with `_U` (spawned subchannel), `start` records the tag's inferred `(S,V,R)` so
  later encode/decode works — see `start_channel_and_infer_types`.
- **`emit(events, status, replace_history)`** — requires started + open. Encode each
  `V`; resolve status (provided → encode + set latest; default → reuse latest). If
  `replace_history`: open a fresh data scope, copy the new batch's closures **and the
  current status closure** in (status is *not* replaced by `replace_history`, so it
  must survive the swap), swap + release the old (the proven `replace_snapshot` swap),
  set `log = batch`; else copy closures into the current data scope + extend `log`.
  Publish one `ChannelUpdateEvent{events, status, replace_history}` to the full hub
  (`origin=data_scope`) and `ChannelState` to the state hub (`origin=data_scope`,
  which holds the status closure; `None` only if status is statically ref-free).
- **`finish(final_value, final_status)`** — requires started + open. Encode + reject
  any service-ref leaf. Copy the final closure **and the latest status closure** into
  `node.durable_scope` (so post-terminal `complete`/`peek`/`state` serve them after
  `data_scope` is released); set `stage="complete"`, `final`, latest status; publish
  `ChannelCompleteEvent` (full, origin `durable_scope`) + `ChannelState(.., "complete")`
  (state, origin `durable_scope`); clear log; release `data_scope`; **run the teardown
  walk over its children** (durable_scope persists until the parent reclaims it).
- **`abort(reason)`** — idempotent: no-op if already complete/aborted. Else copy the
  latest status closure into `node.durable_scope`; set `stage="abort"`; publish
  `ChannelAbortedEvent` (full, origin `durable_scope`, or `None` if status is
  statically ref-free) + `ChannelState(.., "abort")` (state, origin `durable_scope`);
  clear log; release `data_scope`; **run the teardown walk over its children**.
- **`spawn_subchannel(name)`** — mint `key` (`f"{name}-{uuid4}"` / `uuid4`), create a
  child node (unstarted, `_U` types, data scope + hubs), register under
  `node.children[key]`, mount + return `(key, writer_ref)`.
- **`get_tag` / `peek_full` / `peek_state`** — `await node.started` (also woken on
  abort). `get_tag` returns `tag_ref` (copied from `durable_scope`; raises if
  aborted-before-start). `peek_*` build the current snapshot/state, copying closures
  into the call scope from `durable_scope` (tag/final) / `data_scope` (log/status)
  before returning.
- **`subscribe_full` / `subscribe_state`** — retype subscriber to
  `Subscriber[FxtrServiceData]`; if started, seed `[current snapshot/state envelope]`
  via the matching hub with origin = `data_scope` while open (open snapshot / live
  state) or `durable_scope` once complete/aborted (terminal snapshot / final state);
  if not started, subscribe with no seed (the `start` publish reaches them).
- **`open_subchannel_reader(key)`** — raise if *this* node is finished/aborted
  (children are gone — reachable iff parent open); else look up `node.children[key]`
  (missing → `IRException("UnknownSubchannel")`); mount + return a reader for the
  child.

## Helpers + consumer surface

```python
async def create_root_channel[S, V, R](*, tag_type=None, scope=None
    ) -> tuple[ServiceRef[FxtrChannelWriter[ChannelTag[S,V,R], S,V,R]],
               ServiceRef[FxtrChannelReader[ChannelTag[S,V,R], S,V,R]]]: ...
async def start_channel_and_infer_types[S, V, R](untyped_writer, tag, initial_status, *, scope=None
    ) -> ServiceRef[FxtrChannelWriter[ChannelTag[S,V,R], S,V,R]]: ...
def infer_channel_reader_type(reader, as_type) -> ServiceRef[FxtrChannelReader[...]]: ...
```

- `create_root_channel` builds the tree (root node) on `scope`/active, mounts root
  writer + reader, returns the typed pair. `tag_type` only drives the *static*
  types; the actual tag entity is supplied later via `writer.start(...)`.
- `start_channel_and_infer_types(writer, tag, initial_status)` resolves the writer,
  calls `start(tag, initial_status)`, and returns the writer ref retyped from the
  tag's inferred `(S,V,R)` (`replace_service_type`). The producer-side counterpart to
  `infer_channel_reader_type` (the consumer-side retype of a wire-received reader).
- `decode_channel_update(data, *, tag_type, status_type=None, message_type=None,
  result_type=None) -> ChannelUpdate[S, V, R]` — renamed `decode_channel_event`, with
  the status type added and `ChannelTag` inference applied. `tag_type` drives the
  `(S, V, R)` inference (the events themselves no longer carry the tag).
- `iter_channel(reader, scope) -> AsyncIterator[ChannelUpdate[S, V, R]]` (async CM) —
  takes a fully-parameterized `ServiceRef[FxtrChannelReader[T, S, V, R]]`, mounts an
  internal typed `Subscriber[ChannelUpdate[S, V, R]]`, calls `subscribe_full`, yields
  decoded events, unsubscribes on exit. (Consumers needing the tag call
  `reader.get_tag()` — they hold the reader.) (Refs in events are still copied into `scope`
  inside the delivery dispatch so they stay loadable — but now only entity closures,
  never service bridges.) A `iter_channel_state` sibling over `subscribe_state` is
  optional; add only if a test needs it.

## Tests (`v2/tests/test_common/test_channels.py`)

Carry over the stage-13 part-1 suite, adapted, plus the tree/status/subchannel
coverage:

1. `ChannelTag` store/load round-trip; `infer_channel_types` (concrete / subclass /
   bound-generic / rejects generic / rejects non-tag / rejects unparameterized).
2. `ChannelUpdate` / `ChannelState` union decode by `kind`; no event carries a tag
   (tag comes from `get_tag`); seed envelopes == dataclass encoding.
3. Lifecycle happy path: create → start → emit batches (with + without status) →
   full subscriber sees `snapshot_open` then batched `update`s with current status;
   finish → `complete`.
4. State stream: `subscribe_state` / `peek_state` see only `ChannelState`; coalesce
   semantics (latest wins); a status-only `emit` updates state + appears as an empty-
   `events` full update.
5. Mid-stream replay folds equal to from-start (full); post-finish subscribe →
   `snapshot_complete` only; abort on tree/writer teardown reaches live subscribers +
   post-abort snapshot.
6. **Pre-start window + abort-before-start**: `subscribe_*` before `start` deliver
   nothing until `start`; `peek_*`/`get_tag` block until started-or-aborted. A channel
   aborted before ever starting → `peek_*`/`subscribe_*` deliver the statusless
   `ChannelAbortedBeforeStart` variant (no block/raise); `get_tag` raises.
7. **Subchannels + reachability**: `spawn_subchannel` → emit the key on the parent →
   consumer `open_subchannel_reader(key)` → independent lifecycle; unknown key raises;
   keys are per-parent. **Parent finish/abort runs the teardown walk**: an open child
   gets a clean `ChannelAbortedEvent`; after the parent finishes/aborts,
   `open_subchannel_reader(key)` on it **raises** (child reclaimed — reachable iff
   parent open), and a finished child's final is no longer queryable. Recursion: a
   grandchild is torn down when its parent (a child) is torn down by the walk.
8. **Writer-release abort**: dropping a started-but-unfinished writer (its mount scope
   drains) aborts the node; a finished writer's drop is a no-op.
9. Residency: messages carrying entity refs from a transient source scope stay
   loadable by a late subscriber (retention); final value's refs stay loadable after
   finish.
10. Errors: emit/finish before start; double start; double finish; emit after finish;
    `replace_history=True` resets the new-subscriber snapshot and pushes a live update.
11. RPC path: drive `start`/`emit`/`finish`/`spawn_subchannel`/`open_subchannel_reader`
    through resolved proxies from a sibling scope (not just local), with entity refs in
    payloads.
12. Typed subscriber via `iter_channel` receives real `ChannelUpdate` dataclasses with
    decoded `V`/status (tag fetched separately via `get_tag`); `finish` rejects a
    service-ref-bearing final.

Reuse the harness patterns from the old `test_streams.py` / `test_subscription_util.py`
(mount scope + sibling caller + bridge).

## Implementation checklist

**Chunk 1a — surface + smoke gate** (→ pause):
- [x] Module skeleton `fxtr2/common/channels.py`: `FXTR_STREAMS_NS` (kept) +
      writer/reader type ids, `FxtrChannelData` (no service arm), `ChannelTag` +
      `infer_channel_types`, the state/event/snapshot dataclasses + `ChannelUpdate` /
      `ChannelState`, the two service protocols. Delete `streams.py`; test-module
      skeleton.
- [x] Update `nix/fxtr_lib_type_ids` to re-export `CHANNEL_READER_TYPE_ID`.
- [x] Smoke gate: `ChannelTag` + `DataclassEntity` multiple-inheritance round-trip;
      `ChannelUpdate` union decode by `kind`. (Don't build past failures.) ✓ 16 tests
      pass. The self-referential `spawn_subchannel`/`open_subchannel_reader` return
      annotations resolve under plain `@service_protocol` decorators thanks to
      `_resolve_method_hints` injecting the owning class by name (already upstream).

**Chunk 1b — engine** (→ pause):
- [x] `_ChannelTree` / `_ChannelNode` + per-node `durable_scope` + `data_scope`
      holder tasks + the two hubs per node + the explicit teardown walk
      (`finish`/`abort`/tree-close → reclaim children).
- [x] `_WriterImpl` (with abort-on-teardown lifespan) + `_ReaderImpl`; all method
      behaviors above.
- [x] Helpers: `create_root_channel`, `start_channel_and_infer_types`,
      `infer_channel_reader_type`, `decode_channel_update`, `iter_channel`.
- [x] 2 inline smoke tests (lifecycle happy path; subchannel spawn→open→cascade). ✓
      18 tests pass. **Design notes from impl:** (1) the impls are *untyped
      data-shufflers* (declare the `FxtrChannelWriter/Reader[Entity, FxtrServiceData,
      …]` specialization; no `_own_full_type_for_serialization`) — the typed
      proxies on each end do all encode/decode, so the impl never needs per-node
      codecs or runtime `infer_channel_types`. (2) The tag is held/forwarded as a
      bare `FxtrID` (the typed reader proxy reattaches `EntityRef[T]` on `get_tag`)
      — `EntityRef[Entity]` as an impl arg annotation fails the decode strategy.
      (3) mount-on-call-scope + return works (no manual bridge); the writer's
      `_fxtr_mount_lifespan` teardown aborts-if-open, gated by a tree `_closing`
      flag so the tree-close teardown owns full reclaim.

**Chunk 1b-typing — precise helper overloads** (separate step, before tests):
- [x] Give `create_root_channel` (2 overloads) / `start_channel_and_infer_types` /
      `infer_channel_reader_type` precise static return types via TypeVars solved
      from the `tag` / `tag_type` / `as_type` params (the old `FxtrOutputChannel.open`
      shape), so downstream `emit`/`finish`/`iter_channel` typecheck. Bodies keep the
      runtime `cast(Any, ...)` alias construction. ✓ Verified with `reveal_type`:
      a concrete tag yields `ServiceRef[FxtrChannelWriter[ChannelTag[S,V,R], S,V,R]]`
      etc.; the no-tag `create_root_channel()` yields the `Never` (`_U`) placeholder.

**Chunk 1c — full suite + docs:**
- [x] Full suite in `tests/test_common/test_channels.py` (33 tests): smoke gates +
      inference, lifecycle / mid-stream replay / status-only + replace_history /
      pre-start window / residency / subchannels (unknown key, per-parent, cascade,
      writer-release abort) / lifecycle errors / data-only service-ref rejection /
      envelope-pin / iter_channel. `v2/` suite green (2647 passed; one unrelated
      tsv-cli parallel flake, passes in isolation).
- [x] Update CODE_OVERVIEW.md (`common/channels.py` section + file listing) +
      tests/LOCAL_CODE_OVERVIEW.md; fixed stale `fxtr2.common.streams` doc refs in
      `workflows/`.

Note: the dedicated sibling-scope "RPC path" test was dropped — there is no local
fast-path in this design, so every test's `resolve` + proxy call already exercises
the full dispatch/encode/decode path.

**Chunk 1d — abort-before-start (done, recorded here for the record):** a follow-on
chunk after 1c added the `ChannelAbortedBeforeStart` variant + `ChannelStateEvent`
union (a never-started abort is statusless and is delivered on both the full and state
streams so `peek_*`/`subscribe_*` always return a concrete value rather than blocking;
`get_tag` still raises). The settled reasoning is in
[overview.md](overview.md) ("Abort-before-start (settled)"). No further work here.

## Debug subchannel feed (chunk 1e)

A reader-side **debug/fallback** affordance, orthogonal to the primary subchannel API
(apps pick their own key conventions inside messages and call
`open_subchannel_reader(knownKey)`). This feed lets a consumer enumerate a node's
subchannels **without knowing the message schema** — so the frontend's generic
fallback card can surface subchannels for manual tracing (part 2). Custom cards ignore
it.

```python
@dataclass(frozen=True)
class SubchannelOpened:            # data-only (no service ref)
    key: str
    name: str | None               # the debug prefix passed to spawn_subchannel
```

- **A third per-node hub, replay mode.** Alongside the node's full + state hubs, each
  node owns a `subchannels` hub in **replay mode**. `spawn_subchannel` publishes one
  `SubchannelOpened{key, name}` after registering the child. A new
  `subscribe_debug_subchannels` subscriber replays all-opened-so-far, then receives
  live ones — so it is naturally seeded with no separate snapshot type.
- **Append-only during the node's open life.** A child finishing on its own is *not*
  removed from the parent's registry (only the parent's own finish/abort reclaims all
  children via the teardown walk, at which point the node is terminal). So the feed
  only grows while the node is open, and a finished-but-parent-open child stays
  openable (serves its terminal snapshot). The feed therefore needs no removal events.
  On the node's own finish/abort the teardown walk `aclose`s this hub like the others.
- **Independent of `start`.** The feed is about children, not the tag, so it never
  blocks on `start` — a subscriber attached before `start` simply sees the (possibly
  empty) current set and live spawns. `name` is `None` when `spawn_subchannel` was
  called without one.
- The method retypes the subscriber to `Subscriber[FxtrServiceData]` and routes to the
  subchannels hub, exactly like `subscribe_full`/`subscribe_state`; wire dispatch is by
  method name (the frontend passes `"subscribe_debug_subchannels"`).

Tests (extend `tests/test_common/test_channels.py`): replay seeding (subscribe after N
spawns → N `SubchannelOpened`), live delta on a later spawn, `name` prefix carried /
`None`, pre-start subscribe works, hub closed on node finish/abort, per-parent key
namespace reflected. Implemented as its own chunk with `jj pre` + commit
(`3.13post1.1e`); update CODE_OVERVIEW + tests/LOCAL_CODE_OVERVIEW.

- [x] `SubchannelOpened` dataclass + `subscribe_debug_subchannels` on the reader
      protocol/impl.
- [x] Per-node `subchannels` hub (queue mode, seeded per-subscriber from the node's
      `subchannel_log` — `SubscriberHub` has no `replay` mode, so seed-then-live like
      `subscribe_full`); publish on `spawn_subchannel`; `aclose` at finish/abort (the
      feed is done once terminal — children gone, no future spawns; full/state hubs
      stay open) and again (idempotent) in the reclaim teardown walk.
- [x] Tests (6: replay-then-live, name=None, start-independence, per-parent,
      teardown-on-parent-finish, feed-closed-after-terminal); update CODE_OVERVIEW +
      tests/LOCAL_CODE_OVERVIEW. ✓ 47 tests pass.

## Notes to capture during implementation
- Whether `peek_*`/`get_tag` blocking is best as `await node.started` vs a small
  helper that also surfaces abort-before-start as a clean exception/state.
- **Status residency must always have an origin scope for state-hub publishes.**
  Status lives in `data_scope` (shared with the log) for now, with the rule that a
  `replace_history` swap *re-copies the current status* into the fresh scope (it
  drops the log, not the status). Watch for: status being stranded if a swap forgets
  to carry it; old status closures lingering in `data_scope` until the next swap/
  finish (fine for scalar/`None` status — the common case). If `S` routinely carries
  refs and updates often, split out a dedicated 1-element swapped status scope (then
  that scope is the state-hub origin). Defer the split unless it's needed, but keep
  the "status always has a live origin scope" invariant front-of-mind.
- Confirm the per-call-scope mount + service-return bridge keeps a spawned writer
  usable across the scheduler→task arg hop (part 4 depends on it).
