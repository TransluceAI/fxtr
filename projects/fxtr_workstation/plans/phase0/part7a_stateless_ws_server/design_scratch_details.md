# Part 7 — design scratch (details)

Long-form reasoning behind the conclusions in
[`design_scratch.md`](design_scratch.md). Section numbers match.

> **Staleness note (round 2):** §§1, 5, 6, 7 below reflect round-1
> conclusions that were subsequently revised after Daniel's review —
> persistence format is now short-ID-based, ws RPC gained
> connection-scoped transient services and lost subscription replay,
> and the shortId table is persisted in the same storage as sessions.
> The concise doc and the [`design/`](design/) docs are authoritative;
> this file is kept for the alternatives analysis (the rejected
> options and why they were considered).

## 1. Persistence format

The question: when a session's state is written to storage, what
shape is it in? Three candidates:

(a) **Serialize the in-memory atoms directly** (structured-clone-ish
custom format). Rejected: stubs aren't JSON, so we'd be inventing a
second serialization for exactly the values the wire codec already
handles, and the format would drift from the wire format.

(b) **Wire-escape JSON with client short IDs.** Tempting because it's
byte-identical to what the workstation persists server-side. Rejected
because it couples the persisted blob to a particular short-ID table:
the table must be persisted and rehydrated first (ordering
constraint), and a missing/corrupt table breaks every ref in every
blob. (We do persist the table — see §7 — but only for UI-key
stability; with stable identifiers in the blobs it is never
load-bearing for rehydration.)

(c) **Wire-escape JSON with stable identifiers** — entity tags carry
id_strings, service tags carry descriptors, callback/inbox tags carry
inbox UUIDs. Chosen. Properties:

- Rehydration is the same code path as decoding values received from
  the wire: `unescapeWire` with lookups that intern stubs through the
  connector. One codec, exercised constantly.
- The blob is meaningful on its own — debuggable in devtools, could
  even be exported/imported across browsers, since nothing in it
  refers to ephemeral allocator state.
- Slightly larger than short-ID form (id_strings are long). Fine:
  userState is small; the bulky things (entity payloads) are
  deliberately not persisted.

Inventory of what persists, per session: `metadata` + `created_at`,
userState (form above), invoke fills (slot → escaped result/error —
not re-runnable, so they're "unrecoverable" by the agreed rule),
inbox records (UUID, offset bookkeeping, event log in escaped form,
recorded subscriptions for replay), slot counter, dedup maps
(shortId-free form: id_string → slot). Connector-scope: the session
registry (list of session IDs + metadata). Explicitly *not*
persisted: entity load fills (refetch on demand — content-addressed,
so the refetched value is identical; the slot rehydrates as
pending-and-refetching), ws connection state, discovery snapshot
(refetched).

Note on load fills + dedup: the dedup map persists (so a rehydrated
userState whose stubs reference load slots keeps its slot
assignments) while the fill itself refetches. The stub goes through
`pending → filled` again on reload; renderers already handle that
(it's the same as a fresh load).

## 2. Storage library survey

What we actually need: (1) a handful of string-keyed blobs per
session + a registry key, (2) synchronous-ish writes triggered by the
reducer after each commit (debounced), (3) pluggable backend so tests
use memory and apps choose sessionStorage vs localStorage, (4) no
framework coupling.

- **jotai `atomWithStorage`**: syncs *one atom* with storage using
  JSON serialization. Our authoritative atoms hold stub instances
  (not JSON-serializable without our codec) and are written by a
  reducer that must control commit ordering (persist *after* a
  consistent commit, not on every intermediate set). We'd be fighting
  it immediately. Its `createJSONStorage` interface is also
  per-atom, while we want one storage object per connector.
- **zustand/persist**: same shape of mismatch — it persists a zustand
  store, we don't have one and don't want a parallel store next to
  jotai.
- **dexie / idb-keyval (IndexedDB)**: async API (forces the persist
  path async — fine), much bigger quota. We don't need the quota
  because entity payloads aren't persisted; everything else is small
  JSON. IndexedDB's main draw would be storing CBOR payload caches —
  which is a *cache*, not state, and can be added later behind the
  same interface if cold-load latency ever matters.
- **Hand-rolled**: `interface ConnectorStorage { get(key): string |
  null; set(key, value): void; delete(key): void; list(prefix):
  string[] }` plus three trivial impls. The Web Storage API is
  already exactly this; memory is a Map. ~30 lines + tests.

Conclusion: hand-rolled. The interesting design is the *key schema*
and the debounced persist hook in the reducer, not the storage tech.
Suggested key schema: `fxtrhttp:<connector-ns>:sessions` (registry),
`fxtrhttp:<connector-ns>:session:<uuid>:state` etc., where
`<connector-ns>` defaults to a hash of the base URL so two backends
on one origin don't collide.

(If we later want cross-tab live sync on the localStorage backend,
the `storage` event gives change notifications and the interface can
grow a `subscribe` member — noted in open questions #10, not designed
now.)

## 3. Reducer simplification

Walk of `WorkstationSession`'s machinery, sorting each piece into
keep / drop / simplify for the HTTP session:

**Drop** (exists only because a server owns the state):
- JSON-patch diffing (`diffStateToPatch`), patch wire format.
- `ack_seq` / `lastProcessedSeq` / snapshot-vs-delta reconciliation,
  foreign-mutator absorb, stale-event drops.
- Preserve/unpreserve ops and the preserved-seqs no-drift derivation
  (retention is a server-memory concern; client just keeps its data).
- Mutation queue, `MutateSessionRequest`, reject classification,
  optimistic-revert rollback. Writes commit synchronously; the only
  "rollback" left is throwing on invalid allocate (e.g. bad target),
  which happens before commit.
- Track/untrack op derivation (no server pins). We still *walk* state
  (see keep).
- Server-driven lifecycle (`stale` from socket drops; sessions are
  local, ws health is connector-level).

**Keep** (genuinely about the client programming model):
- The ctx-style allocate surface and the same-tick allocate-and-store
  contract (it's the abstract API; renderers rely on it).
- Slot counter, op-result/inbox stub interning, the slot-0 dead
  sentinels, cross-session stub substitution (two HTTP sessions can
  coexist; smuggling rules still apply).
- Load/metadata dedup maps (avoid duplicate fetches; invokes still
  never dedup).
- Inbox logs with offsets + `sliced` cursors.
- The flush-walk that collects stubs in state — repurposed: instead
  of feeding Track ops and preserve calcs, it feeds (a) the
  persistence snapshot, (b) garbage collection of fills/logs whose
  slots no longer appear in state (the client-side analogue of
  retention; without it, fills for discarded slots accumulate in
  storage forever).

**Simplify**: "flush" becomes: run walk → commit local atom →
schedule debounced persist → fire newly-allocated ops (fetch / ws
invoke / inbox mint). No wire frame, no in-flight envelope.

Why fork instead of parametrizing the workstation session: the
workstation reducer's hardest invariants (ack horizons, preserve
no-drift, foreign reconciliation) are exactly the parts that
disappear; a shared base class would either contain them (HTTP
inherits dead complexity) or not (workstation's value gone). The
genuinely shareable leaves — stub classes, escape codec, walk
helpers, inbox log type — are already in or can move to neutral
modules without entangling the reducers. Revisit extraction once the
HTTP session exists and the overlap is observable rather than
predicted (open questions #11).

## 4. Entity load decode path

The workstation's `LoadEntityOp` returns a server-rendered
wire-escape JSON payload. Our server returns raw entity CBOR
(tags 33000/33001/33002) + precomp. Someone must bridge; it's the
client.

Plan: reuse `v2/ts/packages/fxtr-http-client`'s `HttpReadableArena`
(or its internals) for fetch + CBOR decode + precomp ingestion, then
an adapter in the connector that converts the decoded entity value
into `ResultValue`: strong/weak ref leaves → `connector.entityRef`
interned stubs; bytes → `Uint8Array`; the `$fxtr.ctx` marker and
`$fxtr.type` handled the same way the workstation's `load_cbor` →
`wire_escape.escape` path surfaces them. **Parity requirement** (the
real spec): for the same entity, `unescapeWire(workstation_payload)`
and `httpDecode(cbor)` produce structurally identical `ResultValue`s
— this should be a literal test in part 2, run against an entity
served by both a workstation and the TS stateless backend.

Open detail deferred to part 2 planning: exactly which intermediate
representation `fxtr-http-client` exposes (arena `load` returns
codec-shaped values) and whether we use its codec layer or decode
below it. Also where the adapter lives — probably
`@fxtr-ui/connector-http`, importing the core ts package; it must not
drag heavy deps into renderer bundles (connector code is host-side,
so this should be fine).

Metadata: `allocateGetEntityMetadata` answers from the precomp
closure (`unwraps_to`, `ctx_node` → context_data, `backstep`) plus
id_string fields computed locally — same `EntityMetadata` shape as
the workstation. If the needed precomp entry isn't present from prior
loads, fetch `/data/{id}` for the metadata target (the precomp
closure is included with every data response).

## 5. ws RPC protocol

Frames (one JSON object per ws text message):

```
client → server:
  {type: "invoke", id: <string>, service: <name>, method: <string>,
   args: <escaped list>, kwargs: <escaped dict>}

server → client:
  {type: "result", id, value: <escaped>}
  {type: "error",  id, message: <string>, error_type?: <string>}
  {type: "notify", inbox: <uuid>, payload: <escaped>}
```

- `id` is client-minted (uuid or counter-with-nonce); the server
  treats it opaquely and references it exactly once, in the terminal
  result/error. Multiple notifies may interleave before the terminal
  frame, and notifies for long-lived subscriptions continue
  arbitrarily after it.
- `service` is the `name` field from the ws descriptor; one ws URL
  multiplexes all services that share it. Unknown service/method →
  `error`.
- Inbox registration is implicit: when the server unescapes a
  `["callback", uuid]` arg, it records `uuid → this connection` in
  the connection's routing table. `ctx.notify(uuid, payload)` looks
  up the table; if the connection is gone the notify is dropped
  silently (matching the workstation inbox's silent-drop-when-closed
  semantics).
- Ordering: per-connection FIFO is what ws gives us; we promise
  nothing across connections. Notify-vs-result ordering for the
  *same* request follows server emission order (a service that
  notifies then returns will have its notifies delivered first —
  same guarantee the workstation demo relies on with `notify_n`).
- Drop handling, client side: pending invokes reject with a
  retryable-classified error (the session marks those slots errored —
  invoke fills are never silently retried); the connection pool
  reconnects with the workstation connector's backoff schedule;
  on reconnect the session replays recorded `subscribe` invokes
  (see §6) before releasing queued new invokes.
- Keepalive: rely on ws ping/pong at the server framework level if
  available; not part of the JSON protocol. Revisit if proxies bite.

Considered and rejected: reusing the workstation `/client` protocol
subset (drags in subscriptions/sessions concepts the server doesn't
have); JSON-RPC 2.0 (close, but its notification direction and error
object add ceremony without buying interop we'd use).

## 6. `subscribe_to` desugaring

Constraint chain: the dynamic dispatcher (and other existing client
code) calls `ctx.allocateInvoke(inbox.handle, "subscribe_to",
[sourceRef])` — an inbox-targeted invoke. The workstation resolves
this server-side (the workstation owns the inbox impl). In the
stateless world the inbox impl *is the client session*, so
inbox-targeted invokes are handled locally:

1. Validate the handle (same-session, open).
2. Record the subscription `{source descriptor, inbox uuid}` in
   session state (persisted — this is the replay list).
3. Desugar to a normal invoke on the source: `subscribe(handle)` —
   method name matching fxtr2's `EventSource.subscribe(subscriber)`
   convention. The callback tag rides as an arg like any other.
4. The op-result stub fills with whatever the source returned
   (unsubscribe handle, null, anything).

ws source: server registers the inbox on the connection, events flow.
http source: per the agreed model, the server can't do anything with
the callback; a cooperative implementation returns null (or a dummy
handle). No events ever arrive; the dispatcher's reload inbox just
stays quiet. Later, republishing the same dispatch service as a ws
descriptor turns hot reload on with zero client change — the swap is
entirely a discovery-data change.

Unsubscribe: removing the inbox stub from state (the existing
`CloseCallbackOp`-shaped path) drops the subscription records; for ws
sources, send a best-effort `unsubscribe`-method invoke if the source
returned a handle, else just stop routing locally (the server's
routing entry dies with the connection eventually — and stale
notifies to a closed inbox are dropped client-side). This is
deliberately loose; the server holds no durable state to leak.

What this *doesn't* support: server-to-server subscription chains,
durable subscriptions outliving the page. Both are out of scope by
construction (statelessness).

## 7. Short-ID scheme

Requirements: stable across reloads (shell layout persistence keys
panels by shortId), unique within a connector, cheap, and ideally
recognizable in debug UI (the workstation's `d3fa9c` style).

Options:

(a) **Persist the allocator table** (connector-scope storage).
**Chosen.** Uniqueness and stability within a storage scope are
guaranteed by construction — the table *is* the allocation history.
The worry that the table becomes load-bearing state is defused by the
persistence-format decision (§1): session blobs carry stable
identifiers, so a lost/corrupt table costs only cosmetic UI keys
(persisted panel layouts stop matching), never rehydration.

(b) **Deterministic derivation** (pure function id_string → shortId,
hash-prefix style). Rejected as the *guarantee*: a truncated hash
cannot ensure uniqueness, and any collision-extension scheme is
history-dependent — so determinism fails precisely in the collision
case it was supposed to handle. We keep it as the **allocation
policy** inside (a): allocate `d` + hash-prefix of the data id
(extending on collision against the table), `@<provshort>` suffix for
provenance variants (derived from the prov parent's own short, not a
history-dependent `@N` ordinal), and `s` + canonical-descriptor-JSON
hash prefix for services. With this policy, independently-built
tables (e.g. two sessionStorage tabs) agree except after rare
collisions, so shortIds are *usually* portable across scopes while
the persisted table makes them *always* stable within one.
**Correction (2026-06-14):** this hash policy diverged from the real
`short_ids.py` — entities now use a *prefix of the `id_string`* and
services an ascending `s{N}` counter; see the consistency audit in
`part3_connector.md`.

(c) **shortId = full stable identifier.** Maximally simple and
stable, zero collision logic. Rejected mainly for UX: shortIds leak
into visible UI (panel titles, debug views, drag data) and 80-char
id_strings there are hostile; also `JsonRefTree` / link rendering
assume short-ish labels.

Notes: the prefix policy means `classifyShortId` (shell resolver) can
work syntactically (`d…` vs `s…` prefix), same as today. Storage
scope for the table: the shell's layout persistence lives in
`localStorage`, so the table should ideally be durable
(`localStorage`) even when sessions use `sessionStorage` — otherwise
layouts outlive the table they're keyed by. Suggest the connector
takes a session-storage handle and a durable connector-storage handle
separately (or one storage with a per-key durability hint); decide in
part 2.

## 8. Shell ↔ connector interface

From the shell survey, every connector touchpoint in
`@fxtr-ui/workstation` today:

- `ShellRefStubResolverProvider`: `entityRef`, `serviceRef`,
  `listEntities()`/`listServices()` snapshots for `classifyShortId`.
- Status header: `connectionStatus`, `url`.
- Panel/dispatcher session wiring (demo apps, not the shell package
  itself, but the demo is the template): `createSession`,
  `attachSession`, `store`.
- Workstation debug panel: resource lists **with `{debug: true}`
  projections** and `rpc_connections` — workstation-only.
- Session debug panel: raw `connector.connection.registerSubscription`
  for `session_debug` — workstation-only.
- `loadEntityData` (metadata views / JsonRefTree direct loads).

So the portable core is the ~10-member `UiConnector` sketched in the
concise doc. The two debug surfaces feature-detect: e.g.
`interface WorkstationDebugCapable { listRpcConnections(...); ... }`
with an `isWorkstationDebugCapable(connector)` guard; panels render a
"not supported by this backend" placeholder otherwise. Record shapes:
define `UiEntityRecord` / `UiServiceRecord` as the intersection both
backends can honestly fill (`short_id`, `external_name`, maybe
`type_id_string?` later) and have the workstation records satisfy
them structurally (they already carry more fields; TS structural
typing does the rest — no adapters needed on the workstation side).

Where it lives: `@fxtr-ui/core` already hosts the abstract `Session`
and stubs, and the design notes say connectors deliberately *don't*
share a constructor-side interface — this is narrower: the
*consumer-facing* surface. Putting `UiConnector` + `ReactiveList` in
core keeps the shell free of any `connector-*` dependency. The
`{debug}` option on the workstation's list methods stays on the
workstation connector type only.

Migration: change `@fxtr-ui/workstation` imports to core types,
delete its `connector-workstation` dependency, fix the two debug
panels to feature-detect. Demos keep instantiating concrete
connectors (that part was always app-level).

## 9. Backend authoring surface

Goal from the sketch: "writing the backend should feel like (and
mostly be) using Starlette/Hono with helpers". So the library is
*not* a framework — it's:

- **Codec helpers.** `unescape_args(body, *, scope)` → resolves
  `["entity", id_string]` to FxtrIDs the scope can load (error if
  unknown — the server can never fetch client data), passes service
  descriptors and callback UUIDs through as typed values
  (`ServiceDescriptor`, `CallbackAddress`); `escape_result(value, *,
  ...)` does the reverse (FxtrID/EntityRef → entity tag, descriptor
  objects → service tag). Mirrors `wire_escape.py` / `escape.ts`
  with the identifier swap; probably shares code with them where the
  languages allow.
- **Simple-service adapter.** `simple_service_route(impl)` where
  `impl` is `async (method, args, kwargs, ctx) -> value` or a
  `{method_name: fn}` map → returns a Starlette `Route` / Hono
  handler that does GET-query/POST-body parsing, codec, error→status
  mapping. The route path is the service's URL; the author registers
  it and writes the matching descriptor into discovery by hand (or
  via a tiny registry object that builds the discovery payload —
  worth having so discovery can't drift from routes).
- **ws endpoint.** `ws_services_endpoint({name: impl})` → one
  endpoint owning accept/read-loop/dispatch; `ctx.notify(callback,
  payload)` routes via the per-connection table; impl exceptions →
  `error` frames. Concurrency: each invoke runs as its own task so a
  slow method doesn't head-of-line-block the connection (matches user
  expectations from the workstation).
- **Entity/discovery glue.** The existing `create_fxtr_app(scope,
  pins)` / `createFxtrApp` mounts as-is; discovery is a tiny handler
  over the registry object.

Tester scripts (one per language) expose the canonical kitchen sink:
a few pinned entities (including a renderer entity + a type entity so
dynamic dispatch is exercisable), an echo GET service, a POST service
that returns entities + an undiscoverable service descriptor, a ws
service with `subscribe`/`notify_n`-style methods, and a `fail`
method. The TS one doubles as the frontend integration-test backend
(spawned like `_demo_workstation.py` is today); the cross-backend
smoke test drives both scripts with the same scripted HTTP/ws
exchanges and diffs the responses.
