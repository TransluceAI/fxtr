# Stage 7-pre-1 — Metadata loads (entity + service)

A targeted addition that plumbs the workstation's existing
`GetEntityMetadataOp` / `GetServiceMetadataOp` into the frontend.
Modeled directly on entity-load: both ops are deterministic, so the
client caches the slot in wire-visible dedup state and shares one
`OpResultStub` across every allocate call for the same `shortId`.

Pulled forward before stage 7 (dispatch) because type-driven entity
dispatch needs a way to fetch an entity's type before deciding which
renderer to mount — and that's exactly what `EntityMetadata` carries
(see `id_string`, `unwraps_to`, etc.).

## Scope

Two new methods on `FxtrSessionUpdateCtx` (in `@fxtr-ui/core`):

- `allocateGetEntityMetadata(ref) → OpResultStub<EntityMetadata>`
- `allocateGetServiceMetadata(svc) → OpResultStub<ServiceMetadata>`

Their wire behavior mirrors `allocateLoad`:

- Slot is allocated via the existing monotonic `localSlotCounter`.
- Slot is cached in a wire-visible top-level envelope field
  (`entityMetadataDedup` / `serviceMetadataDedup`) keyed by `shortId`.
- Once allocated and referenced from state, the slot's fill event
  is preserved by the existing preserve-set machinery; subsequent
  `allocateGetEntityMetadata` for the same `shortId` returns the
  same stub.
- Same slot machinery (broken-stub handling, cross-session
  substitution, snapshot rebuild) applies unchanged — the stub
  class (`WorkstationOpResultStub`) is already general over op kind.

**Naming.** Verbose `allocateGetEntityMetadata` rather than
`allocateEntityMetadata` to match the underlying op names
(`get_entity_metadata` / `get_service_metadata`) and to leave room
for a future `allocateEntityMetadata(...)` that might do something
fancier (e.g. an aggregate over multiple workstation ops). This
follows the `allocate{verb}` pattern of `allocateLoad`.

**Cache key shape.** Two separate wire-visible dedup maps
(`entityMetadataDedup` / `serviceMetadataDedup`), not a combined
one. Entity and service short_ids live in disjoint namespaces on
the workstation and produce distinct stub return types, so a
combined map would either need a kind discriminator in the key or
sacrifice type guarantees. Two maps keeps the structure symmetric
with `loadDedup` and lets `computeOps` dispatch on spec kind
without inspecting the key.

**Cross-allocator dedup.** Even when `loadDedup[shortId]` already
has a slot for an entity, `allocateGetEntityMetadata(shortId)` mints
a fresh slot in `entityMetadataDedup`. Different ops with
different result shapes — a slot used for a load can't be reused
for a metadata get. This is the same reason callback / invoke
slots aren't shared with load slots.

## Server-side change: wire-escape metadata payloads

The workstation today emits metadata ref fields as plain short_id
strings (e.g. `"unprovenanced": "da1b2c"`). For consistency with
load / invoke / callback payloads — which list-escape every leaf
ref as `["entity", short_id]` so the client can resolve to a
`EntityRefStub` through the standard `unescapeWire` pipeline — we
switch metadata events to the same wire shape as part of this
change.

This avoids two ugly alternatives:

- Frontend special-cases the metadata decode path to interpret bare
  short_id strings at known field names — fragile (server can add
  fields without protocol churn) and forks the codec.
- Frontend leaves metadata refs as bare strings and forces consumers
  to call `connector.entityRef(shortId)` manually — extra ceremony
  for every read.

After the change, every "this is a ref" field in metadata is wire-
escape-tagged at the source, and the client's `unescapeWire` produces
ready-to-use `EntityRefStub` instances. The metadata decode path
becomes structurally identical to load / invoke result decode.

### Backend changes

**`fxtr_workstation/state.py`**

- `_compute_entity_metadata` — fields that currently emit a short_id
  string emit a tagged list instead:
  - `unprovenanced`: `["entity", short_id]`
  - `provenance_parent`: `["entity", short_id]`
  - `unwraps_to`: `["entity", short_id]`
  - `context_data`: `["entity", short_id]`
  - `backstep`: `["entity", short_id]`

  Fields that stay as plain strings (not refs):
  - `id_string`, `provenance_parent_id_string`, `unwraps_to_id_string`,
    `type_id_string`.

- `_compute_service_metadata` — `type_id_string` stays plain. No ref
  fields today, but the pattern is fixed for any future
  service-ref-typed field (`["service", short_id]`).

- We do **not** run `wire_escape.escape(metadata, ...)` over the
  whole dict. The metadata shape is finite and we know which fields
  are refs; emitting pre-tagged lists directly is simpler and
  doesn't require materializing `EntityRef` Python objects for the
  ref fields. The metadata dict is JSON-serializable as-is (tagged
  lists are just lists of strings).

  Caveat for forward-compatibility: if a future field ever needs to
  carry a *literal list* as one of its values, that field will need
  to wrap with the `[[...]]` escape. None today. Note this in the
  docstring.

- Update `EntityMetadataResultEvent.metadata` /
  `ServiceMetadataResultEvent.metadata` docstrings in
  `client_protocol.py` to explain that ref values are list-escape-
  tagged. The field type stays `dict[str, Any]` (the values are
  heterogeneous: plain strings + tagged-list refs).

- Update the corresponding docstrings on `GetEntityMetadataOp` /
  `GetServiceMetadataOp` describing the result event wire shape.

### Backend tests

**`tests/test_metadata_6c.py`** — update every assertion of the form
`assert md["unprovenanced"] == ent_short_id` (and equivalents for
`provenance_parent` / `unwraps_to` / `context_data` / `backstep`)
to the tagged form:

```python
assert md["unprovenanced"] == ["entity", ent_short_id]
```

`type_id_string` and the `*_id_string` plain-string fields stay
unchanged.

### Debug UI

`fxtr_workstation/debug_ui/src/tables/SessionSubPanel.tsx` renders
metadata via `<JsonTree value={e.metadata} />`. The tagged lists
will render as nested arrays. No changes needed — the debug UI's
job is structural display, not semantic decoding. Could be enhanced
later to detect the `["entity", ...]` tag and render a clickable
chip; out of scope here.

### `projects/fxtr_workstation/CODE_OVERVIEW.md`

Update the "Mutation ops" section's
`GetEntityMetadataOp` / `GetServiceMetadataOp` /
`EntityMetadataResultEvent` / `ServiceMetadataResultEvent`
descriptions to note the wire-escape change.

## Frontend `@fxtr-ui/core` changes

### New typed metadata shapes

Add typed `EntityMetadata` / `ServiceMetadata` interfaces. These
live in core (not the connector) because they're fxtr-domain
concepts (FxtrIDs, provenance) that any future connector exposing
metadata would surface the same way. Best home is either a new
`metadata.ts` module or appended to `types.ts`. Pick whichever
seems cleaner during implementation.

```ts
export interface EntityMetadata {
  readonly id_string: string;
  readonly unprovenanced?: EntityRefStub;
  readonly provenance_parent?: EntityRefStub;
  readonly provenance_parent_id_string?: string;
  readonly unwraps_to?: EntityRefStub;
  readonly unwraps_to_id_string?: string;
  readonly context_data?: EntityRefStub;
  readonly backstep?: EntityRefStub;
}

export interface ServiceMetadata {
  readonly type_id_string: string;
}
```

Both interfaces are intentionally open-ish (all-optional except
the always-present ones) — the workstation can add fields without
TS breakage. Consumers narrowing further can cast.

### `FxtrSessionUpdateCtx` extension

Add the two new methods alongside `allocateLoad`:

```ts
allocateGetEntityMetadata(
  ref: EntityRefStub,
): OpResultStub<EntityMetadata>;

allocateGetServiceMetadata(
  svc: ServiceRefStub<unknown>,
): OpResultStub<ServiceMetadata>;
```

Docstrings explain the dedup semantics (same wire-visible dedup
pattern as `allocateLoad`). Each fires its corresponding wire op
when the returned stub is first written into state.

### Abstract `Session` interface

**No changes.** The abstract `Session` already doesn't declare any
of the slot-allocation methods — they live only on
`FxtrSessionUpdateCtx`. Verified in `core/src/session.ts`: only
`sessionId`, `store`, `lifecycle`, `userState`, `detach` (and the
TODO'd future `terminate`) are on the abstract surface.

### Public re-exports

`@fxtr-ui/core`'s `index.ts` re-exports `EntityMetadata` /
`ServiceMetadata`.

## Frontend `@fxtr-ui/connector-workstation` changes

### Protocol types (`protocol.ts`)

The four event types already exist (`EntityMetadataResultEvent` /
`EntityMetadataErrorEvent` / `ServiceMetadataResultEvent` /
`ServiceMetadataErrorEvent`). Two adjustments:

- `EntityMetadataResultEvent.metadata` and
  `ServiceMetadataResultEvent.metadata` change from
  `Readonly<Record<string, unknown>>` to
  `{ readonly [key: string]: WireValue }`. This matches the new
  server-side wire shape — values are mix of plain strings + tagged
  ref lists, all `WireValue`-typed.

The two op types (`GetEntityMetadataOp` / `GetServiceMetadataOp`)
in `protocol.ts` already exist and stay as-is. `MutationOp` already
includes them — no changes there.

### `LocalEnvelope` (`session.ts`)

Two new wire-visible fields alongside `loadDedup`:

```ts
export interface LocalEnvelope {
  readonly user?: UserState;
  readonly terminated?: true;
  readonly loadDedup: ReadonlyMap<string, number>;
  readonly entityMetadataDedup: ReadonlyMap<string, number>;
  readonly serviceMetadataDedup: ReadonlyMap<string, number>;
}
```

- `EMPTY_ENVELOPE` initializes both to a shared frozen empty map
  (parallel to `EMPTY_LOAD_DEDUP`).
- `wirePart` projects each onto `state.entityMetadataDedup` /
  `state.serviceMetadataDedup` when non-empty (skipping the key
  for empty maps, same convention as `loadDedup`).
- `decodedAsEnvelope` reads both back from the wire. `decodeLoadDedup`
  is generalized to a `decodeShortIdSlotMap` helper (same logic; one
  function instead of three copies) and called once per dedup field.

### Inverse indexes

Two new session-private fields:

```ts
private committedEntityMetadataSlotShortIds: ReadonlyMap<number, string> = new Map();
private committedServiceMetadataSlotShortIds: ReadonlyMap<number, string> = new Map();
```

Rebuilt by `reindexCommittedState` (alongside the existing
`committedSlotShortIds` for `loadDedup`). Used by the flush walk to
recover the `shortId` for a metadata slot that's already in
committed user state and no longer has a `pendingSlotSpec`.

### `OpSpec` union extension

Add two new arms:

```ts
export interface GetEntityMetadataOpSpec {
  readonly kind: "get_entity_metadata";
  readonly shortId: string;
}

export interface GetServiceMetadataOpSpec {
  readonly kind: "get_service_metadata";
  readonly shortId: string;
}

export type OpSpec =
  | LoadOpSpec
  | InvokeOpSpec
  | CallbackOpSpec
  | GetEntityMetadataOpSpec
  | GetServiceMetadataOpSpec;
```

### Allocator methods

Both new private methods mirror `allocateLoad` exactly, parameterized
on which dedup map / spec kind they touch:

Pseudocode (one structure used twice):

```
allocateGetEntityMetadata(ref):
  assertWritable + assert lifecycle === "connected"
  shortId = ref.shortId

  # Step 1: same-tick dedup
  for (slot, spec) in pendingSlotSpecs:
    if spec.kind === "get_entity_metadata" and spec.shortId === shortId:
      return opResultStubs[slot]

  # Step 2: cross-tick dedup via wire-visible map
  existingSlot = local.entityMetadataDedup[shortId]
  if existingSlot is not None:
    return interned-or-lazily-minted stub for that slot

  # Step 3: cold path
  ++localSlotCounter
  slot = localSlotCounter
  stub = new WorkstationOpResultStub(slot, this)
  opResultStubs[slot] = stub
  pendingSlotSpecs[slot] = {kind: "get_entity_metadata", shortId}
  return stub
```

`allocateGetServiceMetadata` is identical with the symmetric
`serviceMetadataDedup` / `"get_service_metadata"` substitutions.

The two methods are wired into the `updateCtx` ctor block alongside
the existing three.

### Flush walk

`FlushWalkResult` grows two new fields:

```ts
readonly entityMetadataDedup: ReadonlyMap<string, number>;
readonly serviceMetadataDedup: ReadonlyMap<string, number>;
```

The walk's existing `OpResultStub` branch already classifies slots
into `legitimate` / `newWithSpec` / `broken` and collects
`slotsInLocal`. Generalize the dedup reconciliation block:

- Currently the walk inspects `pendingSlotSpecs[slot].kind === "load"`
  and writes into a single `loadDedup` map. Extend to dispatch on
  `kind`: `"get_entity_metadata"` writes into `entityMetadataDedup`,
  `"get_service_metadata"` writes into `serviceMetadataDedup`.
  Invoke / callback specs don't participate in dedup (no behavior
  change there).
- For slots already in committed (no `pendingSlotSpec`), look up the
  `shortId` from the appropriate inverse index. Today the walk only
  consults `committedSlotShortIds` (load); needs to also consult
  the two new ones. Convention: a slot can only be in one dedup map
  at a time (different ops produce different slots), so a single
  lookup chain (load → entity-metadata → service-metadata) finds at
  most one hit.

Walk pseudocode for the dedup reconcile arm:

```
case stub of WorkstationOpResultStub(slot, _):
  ...
  spec = pendingSlotSpecs[slot]
  if spec is not None:
    newWithSpecSlots.add(slot)
    switch spec.kind:
      "load":                  loadDedup[spec.shortId]                = slot
      "get_entity_metadata":   entityMetadataDedup[spec.shortId]      = slot
      "get_service_metadata":  serviceMetadataDedup[spec.shortId]     = slot
      "invoke", "callback":    (no dedup)
  elif slot in committedSlots:
    shortId = committedSlotShortIds[slot]
    if shortId is not None:
      loadDedup[shortId] = slot
    else:
      shortId = committedEntityMetadataSlotShortIds[slot]
      if shortId is not None:
        entityMetadataDedup[shortId] = slot
      else:
        shortId = committedServiceMetadataSlotShortIds[slot]
        if shortId is not None:
          serviceMetadataDedup[shortId] = slot
        # otherwise it's an invoke / callback / etc. slot — no dedup contribution
```

### `flush()` (the outer driver)

Today's flush reconciles `loadDedup` into `local`:

```ts
if (!loadDedupEqual(local.loadDedup, walked.loadDedup)) {
  local = { ...local, loadDedup: walked.loadDedup };
  this.store.set(this.localAtom, local);
}
```

Extend to also reconcile the two new maps. Same helper `mapEqual`
works for all three (the existing `loadDedupEqual` generalizes —
rename or keep the alias).

### `computeOps`

Extend the `new-with-spec` slot loop to dispatch the two new spec
kinds:

```ts
} else if (spec.kind === "get_entity_metadata") {
  ops.push({ kind: "get_entity_metadata", slot, short_id: spec.shortId });
} else if (spec.kind === "get_service_metadata") {
  ops.push({ kind: "get_service_metadata", slot, short_id: spec.shortId });
}
```

Slot monotonicity is already enforced by `localSlotCounter` — the
workstation's `slot > max_slot_claimed` check passes naturally.

### Fill event handling

`handleFillEvent`'s parameter type widens to include the four
metadata kinds:

```ts
LoadResultEvent | LoadErrorEvent
  | InvokeResultEvent | InvokeErrorEvent
  | EntityMetadataResultEvent | EntityMetadataErrorEvent
  | ServiceMetadataResultEvent | ServiceMetadataErrorEvent
```

`applyEvent`'s router (the `event.kind === ...` check in `onFrame`)
adds the four new kinds and routes them to `handleFillEvent`.

`handleFillEvent`'s decode path forks based on the event's payload
field name:

- Load/invoke result events carry `payload: WireValue` → decode
  through `decodeResultPayload` (no change).
- Metadata result events carry `metadata: { [k]: WireValue }` →
  decode through a parallel `decodeMetadataPayload` helper that
  walks `event.metadata` through `unescapeWire`. Since metadata is
  always a dict (never an array / primitive / bytes at the top
  level), the helper can require dict shape and produce a typed
  `EntityMetadata` / `ServiceMetadata` value. (Or just stay
  `ResultValue`-typed and let the consumer-side `T` of
  `OpResultStub<T>` do the narrowing — easier.)
- Both error variants (`*_error`) synthesize an errored `FillRecord`
  with `event.message`, same as `load_error` / `invoke_error`.

Decode failures (malformed payload, wire-tag rejected) fall through
to the existing "synthesize errored fill" path — no behavior
change in failure handling.

### Snapshot rebuild

`rebuildSlotFillsFromSnapshot`'s event-kind filter includes:

```ts
"entity_metadata_result" | "entity_metadata_error"
| "service_metadata_result" | "service_metadata_error"
```

The decode helper picks up the new metadata path.

### Broken-stub / preserve-events / cross-session substitution

All work as-is. The slot machinery is general; `WorkstationOpResultStub`
doesn't care what op produced the slot. Preserve-events derive from
seqs in `slotFills` — no changes.

## Tests

### Unit tests

New file `tests/metadata_reducer.test.ts`, structured like
`invoke_reducer.test.ts` (the slot machinery is shared, so we don't
need full edge-case coverage of every scenario from
`slot_reducer.test.ts`):

- **Allocate + write → op emitted.** `ctx.allocateGetEntityMetadata(ref)`
  followed by storing the stub in state, emits
  `GetEntityMetadataOp(slot, shortId)`. Equivalent for service.
- **Dedup, same tick.** Two `allocateGetEntityMetadata(same ref)`
  inside one updater return the same stub; single op emitted at
  flush.
- **Dedup, cross-tick.** Second `allocateGetEntityMetadata(same ref)`
  in a later tick (after commit) reads from
  `local.entityMetadataDedup`; same stub, no op emitted.
- **Cross-allocator independence.** `allocateLoad(ref)` and
  `allocateGetEntityMetadata(ref)` for the same shortId produce
  distinct slots (load vs. metadata can't share). Verified by slot
  inequality on the returned stubs.
- **Wire shape.** Allocate, write, flush: outgoing
  `MutateSessionRequest` carries `state.entityMetadataDedup =
  {shortId: slot}` in the JSON patch alongside the user-state add.
- **Fill events → stub.result.** `EntityMetadataResultEvent` flips
  `stub.result` to `filled` with a value typed as `EntityMetadata`;
  tagged ref fields decode to `EntityRefStub` instances. Service
  variant gets `type_id_string`.
- **Error events.** `EntityMetadataErrorEvent` / `ServiceMetadataErrorEvent`
  flip `stub.result` to errored carrying `event.message`.
- **Snapshot rebuild.** Snapshot with preserved metadata events
  rebuilds `slotFills` so the attached stub reports filled.
- **Decode-error path.** Malformed metadata payload (e.g. an entity
  ref tag with a missing shortId) synthesizes an errored fill.
- **Slot-counter tripwire.** Fill event with `slot > localSlotCounter`
  throws (same as load/invoke).
- **Foreign-mutator dedup.** Session A allocates entity metadata,
  commits. Session B (attached same session_id) calls
  `allocateGetEntityMetadata(same ref)` → returns a stub at A's
  slot, no new op. Same flow as the existing foreign-load test.
- **Attach-session dedup bootstrap.** Detach + reattach to a
  session whose committed state has metadata slots; first
  `allocateGetEntityMetadata` on the new session reuses the slot
  from `entityMetadataDedup`.

### Live integration tests

Add to `frontend_lib/packages/fxtr-ui-connector-workstation/tests/integration.test.ts`:

- **Entity metadata happy path** (against `e-test`): allocate,
  store, fill arrives with `id_string` and `unprovenanced` (decoded
  as an `EntityRefStub`).
- **Service metadata happy path** (against `s-test`): allocate,
  store, fill arrives with `type_id_string`.
- **Dedup**: two `allocateGetEntityMetadata` for the same shortId
  share the same stub; a single
  `GetEntityMetadataOp` reaches the server (verifiable via
  `session_debug`).
- **Layered entity metadata** (optional, depends on whether
  `_demo_workstation.py` registers a layered FxtrID — it might
  already; check during implementation): metadata for a layered
  entity carries `provenance_parent` and `unwraps_to` as
  `EntityRefStub`s; further `allocateLoad(metadata.unprovenanced)`
  loads the unwrapped entity end-to-end.
- **Invalid short_id rejection**: allocate against `"does-not-exist"`;
  workstation rejects at op-time with `validation_failed`;
  `validation_failed` canary fires; stub stays `pending`. (Same
  shape as the existing invalid-short-id load test.)

The existing `_demo_workstation.py` is sufficient — no Python
changes; metadata queries workstation-internal record state, not
user-defined methods on `_TestServiceProto`.

## Open questions / things to flag

- **Metadata field for entity type.** The current
  `EntityMetadata` doesn't carry an explicit "this is what kind of
  entity this is" field — `id_string`, `unprovenanced`, and
  `unwraps_to` together let the consumer infer the data-tag, but
  for stage 7 dispatch we may eventually want a more direct
  "rendered type ID" field. Out of scope here; punt to stage 7.
- **Future: metadata refresh.** Today's dedup says "if you've
  asked once, you'll never re-ask while the slot is in state."
  Acceptable for content-addressed metadata (it doesn't change).
  If a future metadata field becomes mutable, we'll need either
  cache invalidation or a non-cached `force=true` variant. Punt.
- **`ServiceMetadata` is thin today.** Only `type_id_string`. The
  shape exists so future fields slot in without API churn — same
  rationale as the Python side. The single-field interface is
  legal TS, but worth a note in the docstring.

## Implementation order

The plan is small enough to land as one PR — no chunking. Rough
execution order:

1. Server-side wire-escape change in `state.py`'s
   `_compute_entity_metadata`; update docstrings on
   `client_protocol.py`; update `tests/test_metadata_6c.py`
   assertions. Run Python test suite green.
2. Update `projects/fxtr_workstation/CODE_OVERVIEW.md` for the wire-
   shape change.
3. `@fxtr-ui/core`: add `EntityMetadata` / `ServiceMetadata` types;
   add the two methods to `FxtrSessionUpdateCtx`; re-exports.
4. `@fxtr-ui/connector-workstation`:
   - Protocol types (`metadata` field typing as `WireValue` dict).
   - Envelope dedup fields + `EMPTY_ENVELOPE` + `wirePart` +
     `decodedAsEnvelope` + the generalized
     `decodeShortIdSlotMap` helper.
   - Inverse-index private fields + `reindexCommittedState`
     population.
   - `OpSpec` union arms.
   - `allocateGetEntityMetadata` / `allocateGetServiceMetadata`
     implementations; wire into the `updateCtx` ctor block.
   - `flushWalk` dedup reconcile generalized to dispatch on
     `spec.kind`; consult all three inverse indexes for committed
     slots.
   - `flush()` reconciles both new dedup maps into local.
   - `computeOps` arms for the two new spec kinds.
   - `handleFillEvent` / `applyEvent` routing for the four new
     event kinds; `decodeMetadataPayload` helper.
   - `rebuildSlotFillsFromSnapshot` event-kind filter extension.
5. Unit tests (`metadata_reducer.test.ts`).
6. Live integration tests (`integration.test.ts`).
7. Update `frontend_lib/CODE_OVERVIEW.md`.

## Post-implementation refactors

Two cleanups landed in the same change-set after the initial
implementation:

1. **Unified reverse index.** The three parallel `Map<slot,
   shortId>` inverses (one per dedup namespace) collapsed into one
   `committedSlotDedupInfo: Map<slot, CommittedSlotDedupInfo>`
   where the value is a tagged `{kind, shortId}`. The flush walk's
   chain-through-three lookups became a single lookup with a
   discriminated-union value.

2. **Unified forward record + single allocator helper.** The
   three top-level wire fields `loadDedup` /
   `entityMetadataDedup` / `serviceMetadataDedup` collapsed into
   one nested `dedup: Record<DedupKind, Map<shortId, slot>>` on
   the wire and in `LocalEnvelope`. The forward namespaces stay
   separate (`entity` / `service` short_ids really are different
   key spaces, no string interpolation), but the regularity is
   exposed for code that wants to iterate over kinds. The three
   allocators all share one `allocateDeduped<T>(operation, kind,
   shortId)` helper (drops the per-kind `getDedup` callback the
   metadata-specific helper had). The wire shape change is
   internal (server stores state opaquely; only client tests
   needed patch-path updates).

## Implementation checklist

- [x] Server: `_compute_entity_metadata` emits tagged ref values
- [x] Server: docstrings on metadata ops + result events updated
- [x] Server: `tests/test_metadata_6c.py` assertions updated
- [x] Update `projects/fxtr_workstation/CODE_OVERVIEW.md`
- [x] `@fxtr-ui/core`: `EntityMetadata` / `ServiceMetadata` typed
- [x] `@fxtr-ui/core`: `FxtrSessionUpdateCtx` extended
- [x] `@fxtr-ui/core`: re-exports updated
- [x] Connector: protocol type updates
- [x] Connector: envelope dedup fields + wire plumbing
- [x] Connector: inverse-index fields + reindex population
- [x] Connector: `OpSpec` extensions
- [x] Connector: allocator methods + ctx wiring
- [x] Connector: flush walk dedup reconcile generalized
- [x] Connector: `flush()` map reconciliation extended
- [x] Connector: `computeOps` arms
- [x] Connector: fill event handling + snapshot rebuild
- [x] Unit tests pass
- [x] Live integration tests pass
- [x] Update `frontend_lib/CODE_OVERVIEW.md`

## Design notes to consult

- `stage2_entity_loading.md` — slot lifecycle, broken-stub handling,
  preserve-set invariant — this stage is structurally additive on
  top of that.
- `stage2_post1_wire_load_dedup.md` — the wire-visible dedup pattern
  is the template `entityMetadataDedup` / `serviceMetadataDedup`
  follow.
- `stage3_invokes.md` — invoke is the closest precedent for
  adding a new `OpSpec` arm + event handling extension.
