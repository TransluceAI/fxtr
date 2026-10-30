# Part 1: Core types — `fxtr2.common.streams`

New package `v2/fxtr2/common/` (currently does not exist) with the
stream system in `v2/fxtr2/common/streams.py`. Tests in
`v2/tests/test_common/test_streams.py`.

Builds directly on `fxtr2.services`: `SubscriberHub`
(`subscription_util.py`), `Subscriber` / `UnsubscribeHandle`
(`subscription_types.py`), `@service_protocol` / `TypedServiceImpl`
(`typed_service.py`), and the dialect codec surface
(`service_dialect.py`'s `encode_to_service_data` /
`decode_from_service_data`).

**Prerequisite:** [part0_protocol_subclassing.md](part0_protocol_subclassing.md)
must land first — this part assumes genuine protocol subclassing
(with inherited-method TypeVar substitution) and the relaxed
`Subscriber` / `EventSource` bounds.

## Namespace + type IDs

```python
FXTR_STREAMS_NS = uuid.UUID("<uuidgen once, hardcode>")

CHANNEL_STORAGE_TYPE_ID = system_entities.define_type(FXTR_STREAMS_NS, "channel_storage")
CHANNEL_SINK_TYPE_ID = system_entities.define_type(FXTR_STREAMS_NS, "channel_sink")
```

One fixed ID per role, shared by *all* channels regardless of
`T`/`V`/`R` — the per-stream type identity lives in the tag entity's
type, not the service type. (Frontend dispatch depends on this:
part 2's preset registers renderers against
`CHANNEL_STORAGE_TYPE_ID.id_string()`.)

## Tag base class

```python
class StreamTag[V, R](Entity):
    """Abstract base for channel-tag entity types.

    Subclassing pins the message type V and final-value type R for any
    channel opened with a tag of this type, so `FxtrOutputChannel.open`
    can infer them (statically and at runtime).
    """
```

- Adds no fields, no `_fxtr_store_impl` — stays abstract. Concrete
  tags subclass both: `class MyTag(DataclassEntity, StreamTag[Msg, Res],
  fxtr_type=define_type(...)): ...`.
- **First implementation step:** smoke-test that mashumaro /
  `DataclassEntity` machinery tolerates the extra generic base
  (store/load round-trip of such a tag). If it doesn't, fallback
  design: `StreamTag` doesn't subclass `Entity` and is a pure marker
  generic; the inference overloads change accordingly. Don't proceed
  past this until verified.

## Event dataclasses + status

Per the sketch, with generic params corrected:

```python
@dataclass(frozen=True)
class OpenChannelSnapshot[T: Entity, V]:
    tag: EntityRef[T]
    values: tuple[V, ...]
    kind: Literal["snapshot_open"] = "snapshot_open"

@dataclass(frozen=True)
class CompleteChannelSnapshot[T: Entity, R]:
    tag: EntityRef[T]
    result: R
    kind: Literal["snapshot_complete"] = "snapshot_complete"

@dataclass(frozen=True)
class AbortedChannelSnapshot[T: Entity]:
    tag: EntityRef[T]
    reason: str
    kind: Literal["snapshot_abort"] = "snapshot_abort"

@dataclass(frozen=True)
class ChannelEmitEvent[V]:
    value: V
    kind: Literal["emit"] = "emit"

@dataclass(frozen=True)
class ChannelCompleteEvent[R]:
    result: R
    kind: Literal["complete"] = "complete"

@dataclass(frozen=True)
class ChannelAbortedEvent:
    reason: str
    kind: Literal["abort"] = "abort"

type ChannelEvent[T: Entity, V, R] = (
    OpenChannelSnapshot[T, V]
    | CompleteChannelSnapshot[T, R]
    | AbortedChannelSnapshot[T]
    | ChannelEmitEvent[V]
    | ChannelCompleteEvent[R]
    | ChannelAbortedEvent
)

@dataclass(frozen=True)
class ChannelStatus[T: Entity]:
    tag: EntityRef[T]
    status: Literal["open", "complete", "abort"]
```

Also include the sketch's `FxtrChannelData` documentation alias
(not enforced at runtime — it documents what `V`/`R` must serialize
into).

**Wire form** (decision 3 in overview): events cross the hub as
`FxtrServiceData` dicts shaped exactly like the mashumaro encoding of
these dataclasses (e.g. `{"kind": "emit", "value": <encoded V>}`).
`tag` encodes as a real `EntityRef` leaf (so the workstation wire
turns it into `["entity", shortId]` for the frontend). The dataclasses
themselves are not registered as entities — they're transient event
payloads, not stored data.

## Service protocols

```python
@service_protocol
class FxtrChannelSink[V](Protocol):
    @service_method
    async def emit(self, item: V) -> None: ...

@service_protocol
class FxtrChannelStorage[T: Entity, V, R](
    EventSource[ChannelEvent[T, V, R]], Protocol
):
    # Genuine EventSource subclass (part 0). `subscribe` is inherited
    # with the substituted typed param
    # `ServiceRef[Subscriber[ChannelEvent[T, V, R]]]`; optionally
    # re-declare it purely to attach the replay-then-live docstring.
    @service_method
    async def status(self) -> ChannelStatus[T]: ...
```

The typed subscribe param is enabled by part 0's bound relaxation +
inherited-method substitution. Mechanically on the impl side: the
storage receives the subscriber ref (wire-decoded to
`UnknownService`, rewrapped by the annotation), **retypes it to
`ServiceRef[Subscriber[FxtrServiceData]]`** (same handle + type_id,
different annotation — use an existing retype helper if one exists,
else construct directly), and hands that to the hub. The hub
publishes pre-encoded dicts (identity encode under the
`FxtrServiceData` annotation); a typed subscriber impl on the
receiving end decodes those dicts into `ChannelEvent` dataclasses via
its own annotation. Untyped subscribers (e.g. the workstation inbox)
just see the dicts.

## `FxtrOutputChannel`

```python
class FxtrOutputChannel[T: Entity, V, R]:
    # -- construction (two-phase) --
    def __init__(
        self,
        channel_tag: EntityRef[T],
        scope: FxtrScope,
        *,
        message_type: Any = None,   # required unless tag type is a concrete StreamTag subclass
        result_type: Any = None,
    ) -> None: ...
    async def mount(self) -> None: ...

    # open is just init followed by mount
    @classmethod
    async def open(cls, channel_tag, scope, *, message_type=None, result_type=None) -> Self: ...

    # -- service refs (raise before mount()) --
    @property
    def sink(self) -> ServiceRef[FxtrChannelSink[V]]: ...
    @property
    def storage(self) -> ServiceRef[FxtrChannelStorage[T, V, R]]: ...

    # -- local writer surface (owner-side, no RPC round-trip) --
    async def emit(self, item: V, *, from_scope: FxtrScope | None = None) -> None: ...
    async def replace_snapshot(self, items: Sequence[V], *, from_scope: FxtrScope | None = None) -> None: ...
    async def finish(self, value: R) -> None: ...
    async def abort(self, reason: str) -> None: ...
```

`from_scope` names the scope to copy/bridge the item's refs from
(retention source); defaults to the channel's scope — **never** the
active scope implicitly (settled at impl time: using a different
source scope invokes a copy/bridge, so it should be explicit; the
docstrings call this out). The RPC sink path uses the dispatch input
scope for the same purpose, so the retention step is parameterized on
a source scope either way. `finish` takes the same `from_scope`
kwarg (same default) for the final value's closure.

`replace_snapshot(items)` swaps the new-subscriber history: existing
subscribers receive nothing; subscribers arriving afterwards see
`items` as the message history (followed by subsequent `emit`s as
normal). Intended for condensing a long history into a smaller
prefix.

Typing overloads on `__init__` / `open` (the inference design,
decision 4):

```python
@overload
def __init__(self: FxtrOutputChannel[T2, V2, R2], channel_tag: EntityRef[StreamTag[V2, R2]], scope: FxtrScope) -> None: ...
@overload
def __init__(self, channel_tag: EntityRef[T], scope: FxtrScope, *, message_type: Any, result_type: Any) -> None: ...
```

(Exact overload shape to be tuned against pyright; the covariant
`EntityRef` makes `EntityRef[MyTag]` match
`EntityRef[StreamTag[V2, R2]]` and solve `V2`/`R2` through the MRO.
Runtime: walk `types.get_original_bases(channel_tag.target_type)`
transitively to find the `StreamTag[...]` specialization; raise a
clear error if absent and explicit types weren't passed, or if the
specialization still contains type variables. The runtime walk is
memoized with `functools.cache` keyed on the tag class.)

`finish` / `abort` in the sketch were sync; making them async is the
honest signature — they publish + drop retention, and `emit` was
always going to be async. Owner-side `emit`/`finish`/`abort` operate
on the shared state directly (same code path as the service impls,
minus wire decode).

### Internal structure

One shared `_ChannelState` per channel, owned by the channel object
and referenced by two thin `TypedServiceImpl`s:

```python
class _ChannelState:
    tag_ref: EntityRef[Any]
    status: Literal["open", "complete", "abort"]
    log: list[FxtrServiceData]          # encoded emitted values, in order
    final: FxtrServiceData | None       # encoded R once finished
    abort_reason: str | None
    hub: SubscriberHub[FxtrServiceData] # mode="queue"
    retention: FxtrScope                # isolated; message residency until finish/abort
    lock: anyio.Lock                    # serializes emit/finish/abort vs subscribe
    msg_encode / result_encode          # dialect codecs from message_type / result_type
```

- `_SinkImpl(TypedServiceImpl[FxtrChannelSink[V]])` — typed dispatch
  decodes `item` into `V`; delegates to the shared emit path.
  `_own_full_type_for_serialization` returns
  `_SinkImpl[message_type]` (generic-impl pattern,
  `typed_service.py:979`). `_service_type_id` →
  `CHANNEL_SINK_TYPE_ID`.
- `_StorageImpl(TypedServiceImpl[FxtrChannelStorage[...]])` —
  `status()` reads state; `subscribe(subscriber)` builds the snapshot
  seed under the lock and delegates to
  `hub.subscribe(subscriber, get_active_scope(), seed=[envelope],
  seed_origin=...)`:
  - open → `[snapshot_open envelope]`, `seed_origin=retention`
  - complete → `[snapshot_complete envelope]`, `seed_origin=` the
    channel's lifespan scope (final-value residency)
  - aborted → `[snapshot_abort envelope]`, `seed_origin=None`
  `_service_type_id` → `CHANNEL_STORAGE_TYPE_ID`.

`mount()`:
1. `scope.attach_lifespan_to_scope(...)` with a CM that opens a task
   group, constructs the hub (`SubscriberHub(task_group=tg,
   mode="queue")`), opens the isolated retention scope, and on
   teardown: if still open, publish `ChannelAbortedEvent(reason=
   "scope closed before channel finished")`; then `hub.aclose()` and
   close retention. (This is `_SchedulerImpl._fxtr_mount_lifespan`'s
   shape, `simple_scheduler/__init__.py:216`.)
2. Mount `_SinkImpl` and `_StorageImpl` into `scope`, stash the two
   `ServiceRef`s.

Emit path (under `lock`):
1. Encode `item` → `FxtrServiceData` via `msg_encode`.
2. Retain residency in `retention`: copy/track the entity closure of
   any `FxtrID`/`EntityRef` leaves (from the dispatch input scope on
   the RPC path, from `from_scope` — default: the channel's scope —
   on the local path) and bridge any `ServiceRef` leaves
   (`bridge_service_from_scope` with
   `keepalive_via="independent_lease"`, matching the scheduler's
   step-bridging; the scheduler's seed-retention tests are the
   behavioral reference). This is also the entirety of sub-channel
   handling: an emitted `ServiceRef[FxtrChannelStorage]` is retained
   like any other service ref.
3. Append encoded value to `log`.
4. `hub.publish({"kind": "emit", "value": encoded},
   origin_scope=retention)`.

Replace-snapshot path (under `lock`): encode `items`, open a *fresh*
isolated retention scope, retain the new items' closures/refs there
(from `from_scope`, default channel scope), swap `log` to the encoded
items and `retention` to the new scope, then close the old retention
scope. Closing it mid-flight is safe — the hub captured per-event
leases at publish time (origin-lease residency), so in-flight
deliveries are unaffected, and the condensed-away messages' resources
are actually released. This is the scheduler's old per-run
retention-swap pattern, repurposed. Raises if the channel is not
open.

Finish path (under `lock`): encode `value`, retain its closure
against the channel's lifespan scope (it must outlive retention),
set `status="complete"` + `final`, publish
`{"kind": "complete", "result": encoded}` with
`origin_scope=<lifespan scope>`, clear `log`, close the retention
scope. Post-finish `emit` raises. Abort path is the same minus the
value (`origin_scope=None`, matching the scheduler's post-drain
`scheduler_closing`).

Snapshot envelopes are constructed manually
(`{"kind": "snapshot_open", "tag": tag_ref, "values": [*log]}`) — a
test pins that this equals the mashumaro encoding of the
corresponding dataclass so typed decode round-trips.

## Consumer helpers

- `decode_channel_event(data, *, tag_type, message_type, result_type)
  -> ChannelEvent[...]` — dialect decode of a wire event dict, with
  the `StreamTag` inference applied when `tag_type` is a concrete
  `StreamTag` subclass. (Lower-level utility; typed subscribers get
  decode for free via their annotation.)
- `iter_channel(storage_ref, scope) ->
  AsyncIterator[ChannelEvent[T, V, R]]` (async CM) — takes a *fully
  parameterized* `ServiceRef[FxtrChannelStorage[T, V, R]]` and pulls
  the decode types from `get_args(storage_ref.target_type)` (clear
  error if the annotation isn't a parameterized `FxtrChannelStorage`
  — wire-received refs must be retyped via `replace_service_type`
  first; settled with Daniel 2026-06-12). Mounts a small internal
  *typed* `Subscriber[ChannelEvent[...]]` impl feeding a memory
  stream, subscribes, yields decoded dataclass events, unsubscribes
  in `finally`. Primarily for tests and Python-side consumers; keeps
  test code honest about the real RPC path and doubles as the
  typed-subscriber proof.

## Tests (`v2/tests/test_common/test_streams.py`)

Use the harness patterns from `test_services/test_subscription_util.py`
(mount scope + sibling caller scope + `bridge_service_from_scope`) and
`examples/fxtr_workstation_demo/tests/test_simple_scheduler_impl.py`
(recording subscriber that loads refs inside delivery).

1. **StreamTag smoke** — concrete tag (`DataclassEntity` +
   `StreamTag[..., ...]`) stores/loads round-trip; runtime V/R
   inference from the ref; explicit-types override; clear error for
   non-StreamTag tag without explicit types.
2. **Lifecycle happy path** — open, emit N, subscriber from the start
   sees `snapshot_open(values=())` + N emits; finish; subscriber sees
   `complete`.
3. **Mid-stream replay** — subscribe after k emits → snapshot carries
   exactly the first k values, then live deltas; fold equals
   from-start fold.
4. **Post-finish subscribe** — only `snapshot_complete`, no message
   values.
5. **Abort on scope end** — scope closes without finish → live
   subscriber gets `abort` event; post-abort subscriber gets
   `snapshot_abort`.
6. **Residency** — messages carrying `EntityRef`s to entities stored
   in a transient caller scope remain loadable by a late subscriber
   after the caller scope closed (retention works); after finish, the
   final value's refs remain loadable but message refs need not.
7. **Sub-channel emission** — emit a message containing another
   channel's `storage` ref; a subscriber receives it and can
   subscribe to the inner channel (replay works through the seed).
8. **Envelope/codec pin** — manual snapshot envelope ==
   mashumaro-encoded dataclass; `decode_channel_event` round-trips
   every event kind.
9. **emit-after-finish raises; double-finish raises;
   replace_snapshot-after-finish raises.**
10. **RPC sink path** — emit via the resolved `sink` proxy from a
    sibling scope (not just the local fast path), with an entity ref
    in the payload.
11. **Typed subscriber** — a `TypedServiceImpl[Subscriber[
    ChannelEvent[Tag, V, R]]]` receives real dataclass instances
    (snapshot + emit + complete), with `V` values and the tag ref
    decoded; exercised via `iter_channel`.
12. **replace_snapshot** — after k emits + `replace_snapshot([c])`:
    an already-attached subscriber received the k emits and nothing
    more (until the next emit); a new subscriber's snapshot carries
    exactly `[c]`; a subsequent emit reaches both; entity refs in the
    condensed-away messages are no longer pinned by the channel
    (old-retention release), while refs in `c` remain loadable.

## Implementation checklist

- [x] Package skeleton: `fxtr2/common/__init__.py` +
      `fxtr2/common/streams.py` with `FXTR_STREAMS_NS` /type IDs,
      `StreamTag`, event dataclasses, `ChannelEvent`, `ChannelStatus`,
      `FxtrChannelData` doc alias; `tests/test_common/` skeleton.
- [x] **Smoke gate** (don't build past failures): StreamTag +
      DataclassEntity multiple-inheritance store/load round-trip ✓;
      `ChannelEvent` union decode by `kind` literal ✓ — the plain
      PEP 695 `type` alias union works with mashumaro directly, no
      `Discriminator` needed (fallback shape would have been the
      `SlotRef` pattern in `fxtr_workflow/entities.py:204`). The
      wire `tag` leaf is a bare `FxtrID` (dialect strips `EntityRef`),
      pinned by `test_channel_event_tag_encodes_as_bare_fxtr_id`.
- [x] Service protocols (`FxtrChannelSink`, `FxtrChannelStorage`) +
      runtime `StreamTag` V/R inference (functools.cache on the
      `get_original_bases` walk).
- [x] `FxtrOutputChannel`: two-phase construction, lifespan CM (hub +
      retention holder + abort-on-teardown), `_SinkImpl` /
      `_StorageImpl`, emit/replace_snapshot/finish/abort paths,
      `__init__`/`open` overloads tuned against pyright (the
      tag-inference overload solves V/R through the covariant
      `EntityRef` and yields `FxtrOutputChannel[StreamTag[V, R], V, R]`
      — the concrete tag type is widened to its `StreamTag`
      specialization, which is fine for everything downstream).
- [x] Consumer helpers: `decode_channel_event`, `iter_channel` (+ its
      typed subscriber impl, type id `iter_channel_subscriber`).
- [x] Update CODE_OVERVIEW.md (new `fxtr2/common/` section).
- [x] Tests 1–12 above (32 tests in
      `tests/test_common/test_streams.py`); `v2/` suite green.
- [x] Update tests/LOCAL_CODE_OVERVIEW.md; final notes below.

## Implementation notes / deviations (post-impl)

- **Abort snapshot needs an origin.** The plan said
  `seed_origin=None` for the aborted-channel seed, but the
  `snapshot_abort` envelope carries the tag's `FxtrID` — an id-bearing
  payload with no origin lease fails at delivery (the hub's documented
  caller-bug). The abort *snapshot* seeds with
  `seed_origin=<channel scope>` (same as complete); only the live
  abort *event* (no ids) publishes with origin `None`.
- **`finish` grew a `from_scope` kwarg** (default: channel scope),
  symmetric with `emit` — the final value's refs are copied into the
  channel scope when they live elsewhere. When
  `from_scope is dest`, retention is a documented no-op (the refs
  must already be loadable from the channel's scope).
- **Same-scope service-ref emission is rejected (footgun closed
  2026-06-12).** Emitting a `ServiceRef` to a service mounted on the
  channel's *own* scope would make the retention scope hold an
  independent lease on that scope's main token; an unfinished channel
  then wedges the scope's graceful close (teardown runs post-drain, so
  the release can't happen first — and the same cycle exists
  transitively for services on *descendant* scopes, since children
  structurally keep parents alive). Emit now raises an informative
  `ValueError` for the exact own-token case (`handle.token is`
  the scope's main token); `finish()` rejects service refs entirely
  (see the next note). We tried the nicer-seeming "skip the bridge in
  emit" first: **unsound** — the retention bridge is also what
  registers the `ServiceKey` on the retention scope, without which the
  per-delivery scope cannot ship the ref to subscribers
  (`mint_lease_for_key` fails). The full fix (also covering the
  descendant-scope variant) needs a small core extension: a
  drain-*started* signal on `FxtrScope` (set at the top of
  `aclose`/`force_close`) that the retention holder can watch to
  release retention as soon as the channel scope begins closing —
  in-flight deliveries stay safe on their per-event origin leases, and
  abort-on-close semantics mean the dropped messages were doomed
  anyway. Proposed, not implemented (touches `entity_ref_scope.py`).
- **`finish()` rejects service refs in the final value (intent
  clarified with Daniel 2026-06-12).** The final value replaces the
  stream as durable, self-contained data — subscribers drop live
  references on completion — so any `ServiceRef` leaf in the encoded
  result raises `ValueError` (emit services as messages instead).
  `FxtrChannelData`'s `ServiceRef` arm applies to `V` only; the alias
  docstring says so now.
- The `ChannelEvent` PEP 695 union decodes by `kind` literal with
  mashumaro directly — no `Discriminator` annotation needed.
- **`iter_channel` retains refs into the consumer scope
  (2026-06-12).** Events were originally handed to the consuming task
  with refs only covered by the (already-closed) per-delivery scope.
  The internal subscriber is now untyped (`Subscriber[FxtrServiceData]`):
  inside each delivery dispatch it copies entity closures and bridges
  `ServiceRef` leaves from the dispatch input scope into the
  consumer-provided scope, *then* decodes (via the `ChannelEvent`
  annotation) and forwards — so everything yielded is usable from
  `scope` afterwards. Cost: one copy per referenced entity, retained
  until `scope` ends (use a transient consumer scope for long
  streams). The typed-subscriber proof moved to an explicit
  `Subscriber[ChannelEvent[...]]` impl in the test file.
- `mount()` requires the calling context to be authorized for the
  channel's scope (it attaches a lifespan); the impls are mounted
  after the lifespan attach, so a mid-`mount()` failure leaves the
  lifespan attached until scope close (harmless: it just aborts an
  unused channel).
