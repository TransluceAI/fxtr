# Stage 13: Output channels — overview

Prototype of the output-channel / stream system sketched in
[sketch.md](sketch.md): a standard interface for plumbing streaming
state out of a computation across RPC, with replay-then-live
subscription semantics, a finish-value that supersedes the message
log, and workstation-frontend rendering dispatched on the stream's
*tag* type.

## Doc map

- [part0_protocol_subclassing.md](part0_protocol_subclassing.md) —
  standalone prep in `fxtr2.services`: enable genuine
  `@service_protocol` subclassing (opt-in registry flag + inherited-
  method TypeVar substitution), relax the `Subscriber` / `EventSource`
  bounds, migrate the existing re-declare workaround sites
  (dispatchers, scheduler proto).
- [part1_core_types.md](part1_core_types.md) — `fxtr2.common.streams`:
  tag base class, event dataclasses, the two service protocols, the
  `FxtrOutputChannel` implementation, consumer helpers, tests.
- [part2_frontend.md](part2_frontend.md) — `@fxtr-ui/rd-types-channel`:
  the two new dispatch keys, the generic no-match stream card, the
  channel-storage service renderers (panel + previews), tests.
- [part3_demo.md](part3_demo.md) — in-place migration of
  `simple_scheduler` in `examples/fxtr_workstation_demo`: per-run
  channels announced over the scheduler's existing EventSource, a
  custom `ChannelContentCard` renderer for the run-tag type.
- [part4_streaming_scheduler.md](part4_streaming_scheduler.md) —
  (stretch) new `streaming_tasks` library + `streaming_scheduler`
  where tasks receive a sink and emit their own progress as nested
  sub-channels.

## Conceptual model (recap)

A channel = **tag** (an `EntityRef[T]` whose *type* identifies the
kind of stream) + a sequence of **messages** (type `V`) + a **final
value** (type `R`) that *replaces* the messages when the channel
finishes. Subscribers joining an open channel get the full message
log as a snapshot, then live deltas. Subscribers joining a finished
channel get only the final value. Channels can appear inside other
channels' messages (sub-computations).

## Key decisions

These were settled during planning; parts 1–4 assume them.

1. **Genuine `EventSource` subclassing (via part 0).** Historically
   the service-resolver registry rejected subtype registration at
   lookup time, forcing protocols to re-declare a wire-compatible
   `subscribe` (`subscription_types.py:109-114`). Part 0 enables real
   subclassing (opt-in `allow_implicit_overrides` on base-protocol
   registration + TypeVar substitution for inherited methods), so
   `FxtrChannelStorage[T, V, R]` subclasses
   `EventSource[ChannelEvent[T, V, R]]` directly, inheriting
   `subscribe` with the substituted typed param. Wire dispatch stays
   by method name, so the workstation's `subscribe_to` callback-target
   invoke (which resolves the target as `EventSource` and calls
   `.subscribe(...)`) works against channel storage unchanged.

2. **Fixed service type IDs.** All channel storages share one fixed
   type ID, all sinks another, minted via
   `system_entities.define_type(FXTR_STREAMS_NS, ...)` in a new
   fxtr2-owned namespace UUID (pattern:
   `HUB_UNSUBSCRIBE_HANDLE_TYPE_ID` in `subscription_util.py`). The
   *renderer* dispatch by tag type happens one level up (decision 5);
   the fixed storage type ID is what lets the frontend recognize "this
   service is a channel" at all.

3. **Event wire form = mashumaro encoding of the event dataclasses;
   typed subscribers via a relaxed `Subscriber` bound.** The
   storage's internal hub is `SubscriberHub[FxtrServiceData]` (queue
   mode) and the log is kept pre-encoded — this is what the
   workstation inbox can deliver to the frontend (wire-escaped JSON
   with real ref leaves), and matches how `simple_scheduler` events
   flow today. The typed surface on top: `Subscriber[T]`'s (and
   `EventSource[T]`'s) `T: FxtrServiceData` bound is relaxed to a
   documented contract ("T must *serialize to* FxtrServiceData") in
   part 0, so the storage protocol carries
   `subscribe(subscriber: ServiceRef[Subscriber[ChannelEvent[T, V, R]]])`
   as sketched (inherited from its `EventSource` base). This works because wire refs drop annotations and
   conversion is asymmetric — sender's annotation drives encode,
   receiver impl's drives decode: the storage retypes the ref to
   `Subscriber[FxtrServiceData]` internally and publishes pre-encoded
   dicts (identity encode); a typed Python subscriber impl decodes
   those dicts into the event dataclasses via its own annotation,
   which round-trips precisely because the wire form *is* the
   mashumaro encoding. `SubscriberHub` keeps its bound (it genuinely
   holds wire values). Channel codecs are built from the
   caller-supplied (or StreamTag-inferred) `message_type` /
   `result_type` annotations via the service dialect's
   `encode_to_service_data`.

4. **`StreamTag[V, R]` type inference.** A `StreamTag[V, R](Entity)`
   abstract base lets a concrete tag type carry its stream's message
   and result types. `FxtrOutputChannel` stays `[T: Entity, V, R]`;
   `open` / `__init__` overloads infer `V`/`R` when the tag ref's
   target type is a concrete `StreamTag` subclass (typechecks because
   `EntityRef` is covariant — `Generic[T_co]`,
   `entity_ref_scope.py:371`), with explicit `message_type=` /
   `result_type=` kwargs as the fallback/override. Runtime inference
   walks `types.get_original_bases` on the target type; generic tag
   classes (unbound params at use site) must pass explicit types.

5. **Tag-type dispatch needs no dispatcher changes.** The renderer
   registered for the fixed channel-storage *service* type (FullPanel)
   fetches `status()` to learn the tag, then re-dispatches
   `<RenderDispatch stub={tagStub} as={ChannelContentCard}
   channel={storageStub}>`. The `ChannelContentCard` key dispatches on
   the tag's *entity* type; the storage ref rides in as a prop. The
   key's NoMatch fallback is the generic stream card (tag reflink →
   streaming message log → result reflink on complete).

6. **Retention = one per-channel retention scope.**
   `hub.subscribe(seed=..., seed_origin=...)` takes a single origin
   scope, so per-emit residency clones don't compose into seeds.
   Instead the channel owns an isolated retention scope (the
   scheduler's `_RunBookkeeping.retention_scope` pattern,
   generalized): `emit` copies/tracks each message's entity closure
   into it and bridges any `ServiceRef` leaves (this is also all the
   sub-channel "keep-alive" there is — emitted channel refs are
   retained like any other service ref; nothing actively subscribes to
   or tracks child channels). `finish` retains the final value's
   closure against the channel's own lifespan, publishes the complete
   event, then drops the message log + retention scope.

7. **Two-phase construction.** `__init__` takes the same args as
   `open` but is inert (no mounting — mounting is async);
   `await channel.mount()` attaches the channel lifespan to the scope
   and mounts the sink + storage impls; accessing `.sink` / `.storage`
   before `mount()` raises. `FxtrOutputChannel.open(...)` is the
   shorthand classmethod that constructs + mounts. If the scope ends
   without `finish()`, the lifespan teardown publishes an abort event
   (the scheduler's `scheduler_closing`-on-teardown pattern).

## Sequencing

```
part0 (protocol subclassing prep)
        │
part1 (fxtr2 core)  ──────►  part3 backend (scheduler migration)
        │                            │
        └──►  part2 (frontend pkg) ──┴──►  part3 frontend (renderers, flake)
                                                  │
                                                  └──►  part4 (streaming_scheduler)
```

Part 0 is a standalone `fxtr2.services` change-set (done separately —
it's independently valuable and independently revertable). Part 1
depends on it and is fully testable with pytest in `v2/`.
Part 2 depends only on part 1's wire shapes (testable with vitest +
mocks). Part 3 wires both into the live demo and is where we "see how
it works". Part 4 is the stretch goal exercising nested channels and
the caller-passes-sink pattern.

## Open questions / verification points

All four resolved during part 1 implementation (2026-06-12):

- ✓ `DataclassEntity` + `StreamTag[V, R]` multiple inheritance:
  mashumaro tolerates the extra generic base — store/load round-trip
  pinned by smoke test.
- ✓ `ChannelEvent` PEP 695 union decode by `kind` literal works with
  mashumaro directly, no `Discriminator` annotation needed.
- ✓ Keepalive mode for bridging emitted `ServiceRef`s into retention:
  `keepalive_via="independent_lease"` (matching the scheduler's
  step-bridging). Caveat discovered: a ref to a service mounted on the
  channel's *own* scope makes retention pin that scope's main token —
  see part 1's implementation notes.
- ✓ Manual snapshot envelopes pinned equal to the mashumaro encoding
  by test. One plan correction: the abort *snapshot* seeds with the
  channel scope as `seed_origin` (it carries the tag's `FxtrID`), not
  `None` — only the live abort event is origin-free.
