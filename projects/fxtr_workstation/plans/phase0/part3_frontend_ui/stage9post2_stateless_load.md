# Stage 9 post-cleanup 2 — Stateless entity-data load on the connector

Plumb the workstation's existing out-of-band `LoadEntityRequest`
into the frontend connector as a plain promise-returning method.
No session involvement, no jotai, no refcounts — the wire op is
already designed for this; we just expose it.

## Motivation

The workstation already supports an out-of-band entity-data load
(`/client` `load_entity` op → `LoadEntityResponse | LoadEntityErrorResponse`),
implemented server-side by `direct_load_worker`. It opens a
transient `load_scope`, runs `load_cbor`, escapes via
`get_or_assign_entity_inactive`, and writes the response back.
Crucially: **no session linkage, no refcounts touched, no
`TrackedEntityRecord` minted for children, and children short_ids
in the payload are descriptors (not lookupable for further direct
loads)**.

`WorkstationServerConnection.sendRequest` already routes the op —
`ResponseFor<"load_entity">` is `LoadEntityResponse | LoadEntityErrorResponse`.
But the frontend currently has no helper that calls it. Consumers
needing to fetch entity data without joining or owning a session
would have to talk to `connection.sendRequest` and decode manually,
which leaks the wire protocol up.

## Scope

One new method on `WorkstationConnector` plus one exported error
class:

- `loadEntityData<T>(stub: EntityRefStub<T>): Promise<T>`
- `class LoadEntityError extends Error` carrying `shortId` and the
  server-supplied `message`.

### Naming

`loadEntityData` rather than `loadEntity` because the operation
deliberately omits the side effects callers usually associate with
"loading an entity" in the workstation system: it does **not**
track the entity, does **not** mint refcounts for children, and the
returned entity ref stubs are display-only descriptors. The name
flags that this is the raw data fetch, not the full session-aware
load.

### Method shape

Argument is an `EntityRefStub<T>` rather than a bare `shortId`
string — callers already work in stub-land and this lets the
return type pick up the stub's type parameter. (A bare-string
overload is trivially addable later if a use case turns up.)

Return type is `Promise<T>` where the resolved value is the
result-mode-decoded payload, structurally a `ResultValue` (same
as in-session load result events). The cast to `T` is unchecked
in the same way the rest of the stub-typed surface is.

### Behavior

```
loadEntityData(stub):
  if this.closed: throw ConnectorClosedError
  response = await this.connection.sendRequest({
    op: "load_entity",
    short_id: stub.shortId,
  })
  if response.type === "load_entity_error":
    throw new LoadEntityError(stub.shortId, response.message)
  return unescapeWire(response.payload, {
    lookupEntity: shortId => this.entityRef(shortId),
    lookupService: shortId => this.serviceRef(shortId),
  })
```

The decode runs in **result-mode** (no `lookupOpResult` /
`lookupInbox`) — server-produced payloads carry only
`ResultValue`-shaped data; encountering a client-only stub tag
would correctly surface as a `WireEscapeError`.

**ServiceRefStub-in-result.** The `unescapeWire` `ResultUnescapeOptions`
interface requires both `lookupEntity` and `lookupService`. We pass
`this.serviceRef` for the latter, but as a system invariant **entities
cannot contain service refs**, so in practice the service-tag branch
should never fire for a `load_entity` payload. This isn't reflected in
the TS types (the structural `ResultValue` admits both kinds); passing
`this.serviceRef` is defense-in-depth that gives a sensible result
(a connector-interned `ServiceRefStub`) if the invariant is ever
violated, rather than throwing.

### Failure modes (all rejections)

- `ConnectorClosedError` — rejected if the connector is already
  closed. (Async function ⇒ promise rejection, not a sync throw.)
- `LoadEntityError` — server-side typed failure (unknown / inactive
  short_id, etc.); carries `shortId` and the server message.
- `WireEscapeError` — payload couldn't be decoded; let it propagate.
- `ConnectionLostError` / `ConnectionClosedError` / `ProtocolError`
  — propagate from `sendRequest` unchanged.

No retry, no caching, no abort signal — each call ships one wire
request.

### Returned-stub contract

The decoded value may contain `EntityRefStub` / `ServiceRefStub`
instances at ref leaves. These are connector-interned (same intern
map the session paths use) for instance equality, **but they are
not server-side tracked or pinned by `loadEntityData`** — the
backend's `direct_load_worker` allocates them via
`get_or_assign_entity_inactive`, which mints a stable short_id
without marking the key active. Two consequences worth documenting
loudly on the method:

1. Calling `loadEntityData(childStub)` on a stub that came out of
   another `loadEntityData` result is **not guaranteed to succeed**
   — the child entity may not be lookupable through the active
   allocator, in which case the server returns
   `LoadEntityErrorResponse`.
2. Callers who want to traverse children should **use the session
   system** (`ctx.allocateLoad`) — that path goes through a
   `TrackedEntityRecord` with proper refcounting and child
   liveness guarantees.

The docstring on `loadEntityData` will state both points
explicitly. (The connector itself can't enforce this — it has no
view into which short_ids are active vs. inactive on the server.)

## Implementation steps

- [x] **Add `LoadEntityError` class** in `connector.ts` next to
      `ConnectorClosedError`. Public; exported from `index.ts`.
- [x] **Add `loadEntityData<T>` method** on `WorkstationConnector`.
      Implementation per "Behavior" above. Docstring covers the
      "returned stubs are descriptors only" contract.
- [x] **Update `index.ts`** to re-export `LoadEntityError`.
- [x] **Update `CODE_OVERVIEW.md`** under the
      `@fxtr-ui/connector-workstation` section's `connector.ts`
      bullet, mentioning `loadEntityData` and `LoadEntityError`
      and the descriptors-only caveat for returned ref stubs.
- [x] **Unit tests** in
      `packages/fxtr-ui-connector-workstation/tests/load_entity_data.test.ts`:
  - Happy path: mock socket replies with `LoadEntityResponse`
    carrying a payload with nested `["entity", ...]` tags;
    resolves with structured value; returned ref stubs are
    reference-equal to `connector.entityRef(shortId)`. (Service
    refs are not part of the test fixture — they can't appear in
    entity data by system invariant.)
  - Server error: mock replies with `LoadEntityErrorResponse`;
    rejects with `LoadEntityError` carrying `shortId` and message.
  - Closed connector: rejects with `ConnectorClosedError`.
  - Closed mid-flight: closing the connector while the request is
    pending propagates `ConnectionClosedError` as a rejection.
  - Malformed payload: `WireEscapeError` propagates as a rejection.
- [x] **Live integration test** in `tests/integration.test.ts`:
      happy-path load of `e-test` (decoded `body` matches the demo
      entity) plus rejection-path with an unknown short_id
      (`LoadEntityError` carrying the bad shortId). The
      `session_debug` refcount sanity check from the original plan
      was dropped — the wire protocol has no session_id field on
      `LoadEntityRequest`, so session refcount independence is
      structural and not interesting to assert against.
