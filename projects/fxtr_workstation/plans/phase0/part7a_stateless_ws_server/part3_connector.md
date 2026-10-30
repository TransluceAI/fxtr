# Part 3 — Client connector (`@fxtr-ui/connector-http`)

The new frontend connector: transport + identity + a client-local
session reducer that emulates a workstation in the browser. Forks the
workstation connector's *concepts*, dropping everything that exists
only because a server owns the state.

Design rationale:
[`design/04_client_connector.md`](design/04_client_connector.md)
(connector/transport/identity),
[`design/05_client_session.md`](design/05_client_session.md) (the
reducer fork — keep/drop inventory),
[`design/03_entity_loading.md`](design/03_entity_loading.md) (entity
decode + metadata), [`design/01_wire_protocol.md`](design/01_wire_protocol.md)
(wire/codec). Part 3 also **builds the kitchen-sink Python tester
backend** (using the Part 2 `service_*` library) in the connector-http
test dir and spawns it over the wire for integration tests.

## Re-orientation pointers

| What | Where |
|---|---|
| Workstation connector (shape we mirror) | `fxtr-ui-connector-workstation/src/connector.ts` (intern maps `:162-163`, refs `:337-355`, `loadEntityData` `:309-327`, sessions `:220-264`, lists `:191-205`) |
| Workstation session (the 3026-line fork source, with keep/drop table) | `fxtr-ui-connector-workstation/src/session.ts` — see design/05's line-by-line fate table |
| Escape codec (extend with resolved mode) | `fxtr-ui-connector-workstation/src/escape.ts` |
| State walks (keep walk, drop diff) | `fxtr-ui-connector-workstation/src/state.ts` |
| Stub classes (fork) | `fxtr-ui-connector-workstation/src/stubs.ts` |
| ReactiveList (HTTP gets its own variant) | `fxtr-ui-connector-workstation/src/resource_list.ts:57-211` |
| HTTP arena client (reuse for decode) | `v2/ts/packages/fxtr-http-client/src/client.ts:76-301` |
| TS CBOR decode + Raw* leaves | `v2/ts/packages/fxtr/src/core/entityCore.ts:351-454` |
| Abstract Session + ctx + stubs | `fxtr-ui-core/src/session.ts`, `fxtr-ui-core/src/stubs.ts` |

## Code sharing decision

Fork-and-simplify (Q-11). Genuinely-shared leaves stay in / move to
neutral modules where cheap: the escape codec gains a **resolved mode**
in `escape.ts` (shared), the stub classes and state-walk helpers are
forked near-verbatim. Do **not** parametrize `WorkstationSession`
(design/05 / scratch §3). Revisit extraction once the overlap is
observable.

## Package layout

New `frontend_lib/packages/fxtr-ui-connector-http/` (in the repo-root
pnpm workspace). Depends on `@fxtr-ui/core`, `jotai`, the core fxtr TS
package + `fxtr-http-client` (host-side decode — must not drag heavy
deps into renderer bundles). Modules in `src/`: `storage.ts`,
`shortid_table.ts`, `escape_resolved.ts` (or extend the shared codec),
`entity_decode.ts`, `ws_pool.ts`, `connector.ts`, `session.ts`,
`stubs.ts`, `resource_list.ts`, `metadata.ts`, `index.ts`.

## Steps

### A0. Shared `Connector` interface → `@fxtr-ui/core` ✅
- [x] Define the abstract **`Connector`** interface (design/06) +
  `ConnectionStatus` in `@fxtr-ui/core`, as a sibling to the abstract
  `Session`. This is the deliberate "now there are two connectors,
  define the common consumer-facing surface" step (the constructor side
  stays per-connector). `HttpConnector` is declared `implements
  Connector`. (`WorkstationConnector`'s conformance + the PanelViewer
  consuming it land in Part 4.) Landed `Connector` + `ConnectionStatus`
  in `core/src/session.ts`; also added `memoryStorage()` (the step-A
  core addition) in `core/src/storage.ts`; both exported from
  `core/src/index.ts`.

### A. Storage — use the standard `Storage` interface ✅
- [x] **No custom storage primitive.** The connector takes the built-in
  Web Storage `Storage` (or `Pick<Storage, "getItem"|"setItem"|
  "removeItem">`); `localStorage`/`sessionStorage` pass in directly.
  Add only **`memoryStorage(): Storage`** (Map-backed, ~6 lines) to
  `@fxtr-ui/core` for tests/SSR — the one thing with no built-in.
  `sessionStorage` is the default; `localStorage` opt-in
  (last-writer-wins multi-tab, Q-10).
- [x] Key schema (design/04): `fxtrhttp:<ns>:table` / `:sessions` /
  `:session:<uuid>`; `<ns>` defaults to a hash of `baseUrl`.
  **Enumeration via the `:sessions` registry blob**, never a key scan
  — so no `list(prefix)` is needed beyond the `Storage` surface.
  (Shapes + keys live in `persistence.ts`; `listPersistedSessions()`
  reads the registry.)
- [x] Persist-failure handling (Q-G): loud `console.error`, keep going
  with in-memory authoritative (every storage read/write is wrapped).
- [x] The same `Storage` handle is shared (by the app) with the Part 4
  PanelViewer layout persistence — see design/04 §Storage. (Demonstrated
  by the Part 5 HTTP demo: `examples/fxtr_http_demo/web` passes one
  `sessionStorage` to both `HttpConnector` and `PanelViewer`.)

### B. ShortId table (`shortid_table.ts`) ✅
- [x] Persisted `shortId → {kind:"entity", idString} | {kind:"service",
  descriptor}` + in-memory reverse indexes. Allocation mirrors the
  workstation's `short_ids.py` (see the consistency audit below):
  entities → a *prefix of the `id_string`* (`d`/`c`/`n` → first 7 chars,
  extended on collision; `.`/`!`/`p`/`u` → full `id_string`); services →
  ascending `s{N}` per canonical descriptor, high-water mark persisted.
  Descriptor canonicalization = absolute url + sorted keys + **drop
  `service_type_id`** (Q-J). (Also scaffolded the
  `@fxtr-ui/connector-http` package + `descriptor.ts` with the
  `ServiceDescriptor` wire type / `canonicalizeDescriptor` /
  `descriptorFromWire` / `descriptorToWire`.)
- [x] Backs `connector.classifyShortId(shortId)` authoritatively from
  the entry `kind` (design/04; needed by Part 4). Unknown → `null`.
- [x] Transient descriptors get entries too (they appear in persisted
  state) and rehydrate born-broken.
- [x] Unit tests (`shortid_table.test.ts`, 10 cases): entity/service
  allocation + idempotence, `service_type_id`-only difference shares a
  shortId, relative≡absolute URL, transient interned, `classifyShortId`
  + unknown→null, persistence round-trip, canonicalization.

### C. Resolved-mode escape
- [x] **Relocate the codec to `@fxtr-ui/core` (the "shared" half).**
  `escape.ts` moved from `connector-workstation` → `core/src/escape.ts`
  (it was already connector-agnostic — operates on the abstract stubs
  via callbacks). Core's `index.ts` exports it; `connector-workstation`
  re-exports it for back-compat and its 4 internal importers now read it
  from core. `tsc --build` clean; all 320 connector-workstation unit
  tests pass (incl. the 71-test `escape.test.ts`).
- [x] Extend the codec so ref leaves have two modes: **shortId mode**
  (persistence — identical to today's `escapeWire`) and **resolved
  mode** (server wire — `["entity", id_string]`, `["service",
  descriptor]`, `["callback", uuid]`). Resolved mode is a pair of
  encode/lookup callbacks the connector supplies, translating
  stub ↔ id_string/descriptor through the shortId table (design/01).
  Done additively, zero churn to connector-workstation: `encodeService`
  widened to return `WireValue` (string short_id *or* descriptor
  object); decode adds an optional `lookupServiceDescriptor` alongside
  the now-optional `lookupService`, and the service-tag decoder routes
  by payload shape (string → `lookupService`, object →
  `lookupServiceDescriptor`). Entity stays a string in both modes;
  callback handle already encoded via a `WireValue`-returning callback.
  Codec tests extended (escape.test.ts, now 76); the connector-side
  resolved-mode opts (stub ↔ id_string/descriptor via the shortId table)
  land with the shortId table (B) + connector (F).

### D. Entity decode (`entity_decode.ts`) + metadata (`metadata.ts`)
- [x] `cborToResultValue(decoded)` — single walk replacing
  `RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID` leaves with
  `connector.entityRef(shortIdFor(innerId))` (allocating shortIds as a
  side effect); bytes stay `Uint8Array`; lists stay lists;
  `$fxtr.ctx`/`$fxtr.type` survive as dict entries → stubs (design/03).
  Done as a pure walk taking an `entityRefForIdString` callback; depends
  on `@fxtr/core` (first `frontend_lib → v2/ts` dep). 6 unit tests.
- [x] Reuse `HttpReadableArena` for fetch+decode+precomp (one per
  connector); **implementation call**: if its `ReadableArena` surface
  drags weight, extract the fetch+decode core (decide while coding).
  → Resolved by **extracting** the core (`entity_data.ts`'s
  `EntityFetcher` — the arena's tracking/scope surface isn't needed
  here); `loadEntityData` / `allocateLoad` / entity metadata all read
  through it.
- [ ] **Parity test** (design/03): same entity via workstation vs the
  Python backend → `unescapeWire(workstation payload)` and
  `cborToResultValue(fxtrCborLoads(/data bytes))` structurally equal.
  → **still open** — needs a cross-backend fixture (one entity served by
  both a workstation and the Python backend). Not closed in Part 5 (the
  HTTP demo exercises the decode path end-to-end, but doesn't compare it
  against the workstation's). Left as the dedicated cross-backend guard.
- [x] `allocateGetEntityMetadata` resolves locally from the precomp
  closure + id_string parsing (unwraps_to, backstep, context_data,
  unprovenanced); fetch `/data/{id}` first if not loaded.
  → Implemented in `connector.ts` (reads the `EntityFetcher` precomp).
  `provenance_parent` is N/A on this wire (bare ids ⇒ the entity is its
  own unprovenanced form, matching the workstation's unprovenanced
  emit). Exercised by the Part 5 demo's metadata views; unit-test
  coverage is light.
  RESOLVED (read the Python source): HTTP entity ids on the wire are
  **always bare data ids** — Python `id_string()` defaults to
  `include_provenance=False`, the service codec escapes with that
  default, and CBOR ref leaves carry only `rawDataId`. Provenance is a
  scope-level layering that never crosses the stateless wire. So
  `provenance_parent` is effectively absent (the `@`-parse stays as a
  defensive fallback), `unprovenanced` is the id itself, and
  `unwraps_to`/`context_data`/`backstep` derive from the data-id tag +
  precomp. Not a design fork — the plan's behavior holds.
- [x] `allocateGetServiceMetadata` resolves `type_id_string` **locally
  from the descriptor's `service_type_id`** (Q-H) — no invoke, no
  "unknown"; error the slot if absent. Done as `buildServiceMetadata`
  (`metadata.ts`); throws `MissingServiceTypeError` → errored slot. 2
  unit tests.

### E. ws pool (`ws_pool.ts`) ✅
- [x] One `WsConnection` per ws `url`: socket, monotonic **generation**,
  pending-invoke map. (Transient *stubs* are session-side, bound to the
  (url, generation) the pool exposes via `isGenerationLive`.)
- [x] **Connect-on-demand** (Q-A): open/reopen when an invoke needs it;
  invokes during a failed attempt reject (→ `invoke_error` fill); **no
  background reconnect**. On drop: pending invokes reject, generation
  bumps, and `onDrop(url, deadGeneration)` fires — the session (§G) uses
  it to break transient stubs of that generation + flip inbox
  `connectionState → broken`.
- [x] notify routing: `{type:"notify", inbox, payload}` → `onNotify(inbox,
  payload)`; the connector (§F) maps the inbox uuid → owning session.
- [x] Transient stubs bound to (url, generation): `invoke(..., {
  expectedGeneration })` routes iff the live generation matches, else
  rejects "transient service broken" (the **at-send** half; the
  at-allocate half is the session's, §G). The `WebSocket` is injectable.
- [x] Unit tests (`ws_pool.test.ts`, 9 cases, mock WebSocket): invoke
  happy/error, connection reuse, notify routing, drop (reject + onDrop +
  generation), reconnect with bumped generation, stale-transient reject
  (no reconnect), live-transient route, close.

### F. Connector (`connector.ts`) + lists (`resource_list.ts`)
- [x] `new HttpConnector({ baseUrl, storage, fetchFn?, webSocketCtor?,
  store?, namespace? })`. Owns store, shortId table (persisted through
  `storage`, debounced), intern maps, `EntityFetcher`, ws pool, discovery
  lists. (Session registry lands with G's `createSession`.)
- [x] `entityRef`/`serviceRef` (interned), `classifyShortId`,
  `loadEntityData` (fetcher → `cborToResultValue`; children **fully
  loadable**), `connectionStatus` (tracks **discovery reachability**:
  `connecting` → `open` on the first successful `/discovery`,
  `reconnecting` on a later `refresh()` failure (no auto-retry), `closed`
  on `close()`; ws-pool drops stay out of it — they self-heal on the next
  invoke and have their own per-service broken status).
  Plus `getEntityMetadata` / `getServiceMetadata` and the session-facing
  hooks (`wsPool`, `registerInbox`/`onConnectionDrop`,
  `markServiceBroken`, `internEntityByIdString`/`internServiceByDescriptor`).
- [x] `GET /discovery` at construction → `listEntities()`/
  `listServices()` `DiscoveryList`s + a `refresh()` (no live deltas);
  a failed fetch flips both lists to `error` status (records retained)
  and `connectionStatus` to `reconnecting`; service records carry
  `typeIdString`. **HttpConnector-specific.**
- [x] `createSession(metadata)` / `attachSession(id)` + **intern one
  live instance per sessionId** (Q-B); `implements Connector`. `close()`
  is idempotent (now also flushes live sessions + table synchronously).
  `createSession` registers + persists an initial blob; `attachSession`
  rehydrates from the blob, and an id it **can't restore** (never
  persisted, unparseable, or a bad-version/corrupt blob) yields a
  terminal **`unavailable`** session — mirroring the workstation
  connector's attach-to-missing, *not* a silently-created one (use
  `createSession` for that). An unavailable session touches no storage
  and isn't interned. `removeSession` (detach) keeps storage,
  `deleteSession` (terminate) removes it; `listPersistedSessions`
  reads the registry.
- [x] Unit tests (`connector.test.ts`, 4 cases, mock fetch + memoryStorage):
  discovery + classifyShortId + interning, load/decode + service
  metadata, table persistence rehydrate, close.

### G. Session reducer fork (`session.ts`, `stubs.ts`)
Follow design/05's keep/drop table. Highlights:

**Approach (decided w/ Daniel):** the HTTP session is distinct enough
from the workstation session that it's a **clean rewrite** (`HttpSession`)
guided by design/05 + the kept algorithms — *not* a copy-and-excise of
`session.ts`. Verify behaviour against the real source (the abstract
`Session` / `FxtrSessionUpdateCtx` contract in `core/session.ts`, the
workstation `session.ts` flush/slot mechanics) rather than the design
doc alone. `stubs.ts` is already forked (separate commit).

**Implementation notes (filled in as we go — guesses / weirdness /
revisit-later):**
- `subscribe_to` direction (clarified from `dynamic_dispatcher.ts:579`):
  the uniform frontend API is `allocateInvoke(inbox.handle,
  "subscribe_to", [source])` — method `"subscribe_to"` *on the inbox
  handle*, source as the arg. The HTTP session **reinterprets** that
  handle-targeted invoke as a **source-targeted** one:
  `allocateInvoke(source, method ?? "subscribe", [inbox.handle])` (calls
  `source.subscribe(myHandle)`). The flip (handle-target+source-arg →
  source-target+handle-arg) is the "desugar". Its fill is whatever the
  source returned (ws: a transient unsubscribe handle, recorded for
  best-effort unsubscribe + `connectionState=connected`; http: inert).

- [x] **Keep**: ctx allocate surface + same-tick allocate-and-store;
  slot counter; op-result/inbox stub interning; slot-0 dead sentinels;
  cross-session substitution; load/metadata dedup (`allocateDeduped`);
  inbox logs/offsets/`sliced`; the flush-walk's stub collection; local
  fill/notify entry points (`fill` / `handleNotify`).
- [x] **Drop**: JSON-patch diff + wire ops; inflight envelope +
  commit/reject/rollback; snapshot/state_update/seq reconciliation;
  preserve/unpreserve + retention horizon; Track/Untrack op derivation;
  server-driven `stale` lifecycle.
- [x] **Write path** (design/05): updaters run with ctx (only failure
  mode is a throw at allocate/encode) → microtask flush-walk substitutes
  cross-session stubs → **local atom becomes committed atomically** →
  fire ops (load→`loadEntityData`, invoke→http fetch / `wsPool`,
  metadata→local, callback→mint inbox committed immediately).
  (Storage persist is the follow-up; `schedulePersist` is connector-side
  for the shortId table only so far.)
- [x] **Local seq counter** stamps fills/inbox entries at delivery.
- [x] **GC**: `reindexCommittedState` — after each commit, drops
  `slotFills`/`inboxLogs` (+ tears down inbox routes / best-effort
  unsubscribe) for slots no longer in committed state.
- [x] **Inboxes**: `allocateCallback` mints slot + uuid, committed
  immediately, route registered at once. `subscribe_to` reinterpreted
  as `source.subscribe(handle)` (see note above). **`connectionState`
  synthesis** (N1): connected on a successful ws subscribe (records
  (url, generation)); broken when that generation drops (via the
  connector's `onConnectionDrop`). **Best-effort `unsubscribe`** on
  inbox close / detach when a live transient handle was recorded (Q-E).
  The notify route is registered at **commit** (in `flush`), upholding
  the workstation's committed-first invariant — an inbox can't receive
  before its slot commits (behavioral audit I4, 2026-06-14). The
  `connectionState=connected` is set only when a live ws generation
  backed the subscribe; subscribing to a non-pushing source (http/GET)
  leaves it `idle`, which is uniform across both connectors and now
  documented on the `CallbackInboxStub` base (behavioral audit I3).
- [x] **Persistence/rehydration (commit 2, 2026-06-15)** (design/05):
  one storage value per session — `{version, metadata, createdAt,
  slotCounter, seqCounter, userState: <shortId-mode escape>, dedup,
  slotFills, inboxes}` (shapes in `persistence.ts`). `serialize()` builds
  it; the session **debounces** a persist (microtask) after each commit /
  fill / notify / sub-state change and flushes synchronously on
  `detach`; the connector owns the `Storage` write. `restoreFrom(blob)`
  (in the ctor) rehydrates: decode userState (state-mode `unescapeWire` w/
  connector lookups + session-scoped slot/inbox lookups); **load +
  metadata fills are not persisted → those slots refire** (identified via
  the dedup reverse index), invoke fills persist as-is, in-flight-at-save
  → errored ("interrupted") (Q-9); inbox logs verbatim; transient stubs +
  previously-`connected` subscriptions rehydrate broken; a corrupt /
  unsupported blob **can't be restored**, so `restoreFrom` returns false
  and the session lands terminal **`unavailable`** (loud `console.error`,
  no crash, no fabricated blank session). (Note:
  the design said *load* fills refire and *invoke* fills persist; metadata
  is folded in with loads — it's local + cheap and needs the precomp
  closure refetched anyway, so it refires too.)
- [x] `detach()` local semantics (drain pending flush → unregister
  inboxes + best-effort unsubscribe → `removeSession` → lifecycle
  `closed`); `assertWritable` gating kept.
  `onRollback` is accepted but ~never fires (remote failures = errored
  fills) — NOTE/revisit: currently the write path ignores `onRollback`
  entirely; revisit if a client-side validation rollback path is wanted.
- [x] **`terminate()` (commit 1, 2026-06-14).** Promoted to the
  abstract `Session` base in `@fxtr-ui/core` (the old TODO there) —
  `terminate(finalState?, options?): Promise<void>`, matching the
  workstation signature so its conformance is unchanged. It's the
  counterpart to `detach`'s "release but keep": the generic
  panel-viewer's terminate-on-panel-close (Part 4) needs it on the
  abstract surface. `HttpSession.terminate` writes `finalState` through
  the normal flush first (subscribers observe it before `closed`), then
  tears down inboxes + best-effort unsubscribe + `removeSession`, and
  ends `closed`; never rejects. **Deletion of the persisted storage
  blob + registry entry lands with persistence (commit 2)** — until
  then `terminate` differs from `detach` only in intent.

Update CODE_OVERVIEW.md (`frontend_lib/CODE_OVERVIEW.md`: new
`@fxtr-ui/connector-http` package — modules, the local-reducer model,
resolved-vs-shortId codec modes, the session-interning deviation).

## Tests

Unit (mock WebSocket / fetch, mirroring connector-workstation's suites):
- [x] escape resolved-mode round-trips (in core's `escape.test.ts`);
  shortId table allocation + collision extension + `classifyShortId`
  (`shortid_table.test.ts`, 15 cases).
- [x] entity decode (`entity_decode.test.ts`, 6 cases) +
  `entity_data.test.ts` (the `EntityFetcher` fetch+decode+precomp core).
  The decode path is exercised end-to-end over the wire by the
  integration suite (load a real entity + traverse its child ref). The
  *strict* cross-connector parity assertion (workstation vs Python
  backend, byte-for-byte structural equality of one entity) is **left as
  a follow-up** — it needs both a workstation and the Python backend in
  one test; the integration decode test covers the Python side.
- [x] session reducer (`session.test.ts`, 18 cases): load/invoke/metadata
  allocate→fire→fill; dedup (same-tick **and cross-tick**, audit I1);
  callback inbox notify; `subscribe_to` desugar **filling the op-result
  with `null`** (audit I2); `connectionState` `connected` on a ws
  subscribe + **`idle` for a non-pushing http source** (audit I3); local
  `connectionState` synthesis on ws drop; best-effort unsubscribe +
  notify-route teardown on terminate; GC of dropped slots; terminate
  finalState/close/registry-drop; **persistence→rehydrate** (committed
  load+invoke round-trip w/ load refetch, invoke interrupted, transient
  born-broken, detach-retains-vs-terminate-deletes, corrupt/unsupported
  blob → fresh).
- [x] connector (`connector.test.ts`, 4 cases): discovery lists + refresh;
  classifyShortId + interning; load/decode + service metadata; table
  persistence rehydrate; close idempotence. Plus `stubs.test.ts` (7
  cases, the forked stub atoms) and `ws_pool.test.ts` (9 cases:
  connect-on-demand, notify routing, drop + generation break, stale
  transient reject). (Session interning is covered in `session.test.ts`.)

Integration — **kitchen-sink Python tester backend (commit 3,
2026-06-15)** built on the Part 2 `service_*` library (a pinned
parent/child entity graph; `echo` GET; `data` POST with
`get_root`/`make_service`/`boom`/`$fxtr.type`; `feed` WS with
`subscribe` emitting ordered notifies + returning a transient
unsubscribe, `boom`, `$fxtr.type`; an undiscoverable hidden GET handed
out by `make_service`) in `tests/_demo_backend.py`, spawned over the
wire by `tests/_integration_harness.ts`:
- [x] connect + discovery; load entity + **child-ref decode + traverse**;
  GET invoke (echo) + POST invoke (`get_root` returns an entity, loaded);
  **undiscoverable returned-descriptor** service invoked; **ws invoke**
  (`$fxtr.type`); callback inbox happy path (**ordered `n0/n1/n2`**) +
  `connected` + **cursor advance**; **invoke error path** (`boom` →
  errored fill); service metadata `type_id_string`; entity metadata from
  precomp; **reload→rehydrate** over the wire. (`integration.test.ts`, 8
  cases; dynamic dispatch covered transitively — no dispatch backend.)
  Transient *unsubscribe* is exercised on inbox teardown (the unsub
  handle is recorded internally, not user-visible), not asserted
  server-side. The harness spawns the backend via **`uv run python`**
  (uv forwards `SIGTERM`), and the backend shuts down **gracefully** on
  the signal (trigger hypercorn's shutdown + let `serve()` return). The
  one prerequisite for a prompt graceful drain is that no server ws read
  loop is left parked — so the integration suite closes every connector
  (an `afterEach` safety net on top of each test's own `close()`), and
  `WsPool.close()` now actually **closes the underlying sockets** (it
  previously only rejected pending invokes, leaking the open socket and
  blocking the drain). An open ws would otherwise hang the drain until
  the harness's SIGKILL fallback.

Update CODE_OVERVIEW.md (final).

## Consistency audit vs. the workstation server + connector

A read-through (2026-06-14) comparing the implemented HTTP connector
against the real workstation short-ID allocator
(`src/fxtr_workstation/short_ids.py`), its entity-metadata resolver
(`state.py:_compute_entity_metadata`, `:4043`), and the Python
stateless wire (`v2/fxtr2/experimental/http/service_codec.py`). The
governing intent (overview §"a workstation in the browser") is that
the connector *presents workstation-style short IDs* and answers
metadata the same way the workstation would; the items below are where
the implementation drifts from that. Each lists **(a)** what is
observably different and **(b)** what the connector would need to do.

**Status: items 1, 2, 3, 5 fixed (2026-06-14).** Entity shortIds are
now a prefix of the `id_string`, services use an ascending `s{N}`
counter (high-water mark persisted), and the dead `@`-split was
removed from `getEntityMetadata`. Item 4 is an accepted divergence
(no code change). Per-item resolution notes inline below.

### 1. Entity short IDs are an opaque hash, not a prefix of the id_string

(a) **Observable difference.** The workstation mints an entity short
ID that is a *literal prefix of the entity's `id_string()`*: for
prefixable tags (`d`/`c`/`n`) the bucket prefix is `full[:7]`
(`short_ids.py:_allocate_bucket`, `entity_short_id_prefix_len=7`),
extended one char at a time on collision; for non-prefixable tags
(`.`/`!`/`p`/`u`) the prefix is the *full* `data.id_string()`. Because
`id_string()` already begins with the tag char (`entity_core.py:336`
`local_id_string`), a workstation entity short ID always (i) starts
with the entity's real tag and (ii) is a genuine prefix you can
eyeball against the full ID — e.g. `da1b2c…` for a `d`-entity,
`cXY…` for a `c`-entity.

The HTTP connector instead computes `"d" + FNV1a(idString)` hex prefix
(`shortid_table.ts:allocate` + `:shortIdForEntity`, `:81-88`,
`:141-150`). So the short ID (i) is **always tagged `d`** regardless of
the entity's real tag (a `c`-node or `!`-builtin shows up as `d…`) and
(ii) bears **no relation to the id_string** — it's a separate hash. The
"short ID is consistent with the actual ID string" property the
workstation guarantees is lost; `RefLink` (which renders the bare
short ID) would display a misleading tag, and the short ID is no longer
debuggable against `/data/{id_string}`.

(b) **To make it consistent.** Allocate the entity short ID as a prefix
of the `id_string` itself, mirroring `short_ids.py`:
- prefixable tag (first char ∈ `{d,c,n}`): `idString.slice(0, 7)`,
  extending one char at a time while the candidate collides with an
  already-claimed prefix (keep the existing `byShortId.has` collision
  loop, but slice from `idString` rather than from a hash, and seed the
  length at 7 to match the workstation default);
- non-prefixable tag (`.`/`!`/`p`/`u`): use the **full** `idString` as
  the short ID (no truncation), as the workstation does.

Drop `hashHex` for entities and drop the extra `"d"` tag prepend (the
tag is already the first char of `idString`). The connector only ever
sees **bare data ids** on the wire (see item 3), so none of the
workstation's provenance suffix machinery (`@N` / `@0` / `@u` /
`@<prov_short>`) is needed — only the data-half prefix policy.

**Fixed (2026-06-14):** `shortid_table.ts:allocateEntityShortId` now
slices the prefix from `idString` (len 7 for `d`/`c`/`n`, full string
for non-prefixable tags), `hashHex` removed, `ENTITY_PREFIX_LEN = 7`.
`shortid_table.test.ts` updated to realistic id_strings + prefix /
tag-preservation / collision-extension / non-prefixable cases.

### 2. Service short IDs are a hash, not an ascending `s{N}` counter

(a) **Observable difference.** The workstation allocates service short
IDs as `s{N}` from a monotonic counter, never recycled for the run
(`short_ids.py:get_or_assign_service`, `:200-219` —
`f"s{next(self._service_counter)}"`). So services are `s0`, `s1`, `s2`,
… in registration order. The HTTP connector computes
`"s" + FNV1a(canonicalDescriptor)` hex prefix
(`shortid_table.ts:shortIdForService` → `allocate("s", …)`). The IDs
are stable per-descriptor but **not ascending** and not in any
discovery order — `s3f9a1c`, `sb2e…`, etc.

(b) **To make it consistent.** Keep an integer counter on the table;
`shortIdForService` mints `s${n++}` on first sight of a canonical
descriptor key (the descriptor→shortId map already gives idempotence).
Because the HTTP table is **persisted and rehydrated** across reloads
(unlike the workstation's run-lifetime counter), the counter's
high-water mark must be **persisted in `SerializedShortIdTable`** and
restored in the constructor, so post-reload allocations keep ascending
without colliding with rehydrated IDs. The canonicalization (drop
`service_type_id`, resolve relative→absolute URL, sorted keys) stays as
is — it's the identity key feeding the counter, and matches the
workstation's "same `ServiceKey` ⇒ same short ID" guarantee.
`shortid_table.test.ts` assertions only check `startsWith("s")` +
idempotence + distinctness, so they survive; add a case pinning the
ascending order.

**Fixed (2026-06-14):** `shortid_table.ts` mints `s${serviceCounter++}`;
`SerializedShortIdTable.serviceCounter` persists the high-water mark and
the constructor restores `max(persisted, maxRehydrated + 1)` (robust to
old persisted blobs that predate the field). Tests added for ascending
order and counter-continuation across rehydrate.

> Note: discovery currently interns services in `Object.entries(payload.services)`
> iteration order (`connector.ts:349-359`). With an ascending counter,
> that iteration order *becomes* the `s{N}` numbering, which is the
> closest analogue to the workstation's registration order — acceptable,
> but worth a comment that the order is discovery-iteration, not a
> server-assigned sequence.

### 3. `getEntityMetadata` provenance `@`-split is ungrounded / dead

(a) **Observable difference.** `connector.ts:getEntityMetadata`
(`:240-276`) splits the id_string on `"@"` and, if found, derives
`provenance_parent` / `provenance_parent_id_string` / `unprovenanced`
from the two halves (`:259-268`). But the Python stateless wire
**never emits a layered id_string**: `service_codec.py:211` escapes
entities with `value.id_string()`, whose default is
`include_provenance=False` (`entity_core.py:443`), and `/data` ids are
likewise bare (`asgi_app.py`). Provenance is a scope-level layering that
does not cross this wire (the part already records this under step D,
"RESOLVED"). So the `at >= 0` branch is **dead code grounded in a guess
about a wire shape that doesn't exist**. Worse, *if* it ever fired it
would diverge from the workstation, which sets
`id_string = layered_fid.local_id_string()` — the **data half only**
(`state.py:4072`) — whereas the connector leaves `meta.id_string` as
the full (would-be layered) `idString` (`:257`).

(b) **To make it consistent.** Drop the `@`-split branch and always
take the `else` path: `id_string = idString` (already the bare local
form on this wire) and `unprovenanced = entityRef(stub.shortId)` (the
entity is its own unprovenanced form). This matches what the
workstation produces for an unprovenanced entity — `unprovenanced`
present, `provenance_parent*` absent — without pretending a provenance
half can arrive. If a defensive guard is still wanted, replace it with
an assertion/`console.error` that the wire id is unexpectedly layered,
rather than silently synthesizing parent refs.

**Fixed (2026-06-14):** `connector.ts:getEntityMetadata` dropped the
`@`-split branch and the `provenance_parent*` fields entirely; it now
always sets `id_string = idString` + `unprovenanced =
entityRef(stub.shortId)`. Docstring updated to record why provenance
never crosses this wire (and that `unwraps_to`/`context_data`/`backstep`
refs are bare — item 4).

### 4. `unwraps_to` ref points to a bare id, not the layered unwrap

(a) **Observable difference.** The workstation's `unwraps_to` ref is a
*layered* entity `(unwrapped_data, prov=data_id)` produced by
`embed_as_provenance_parent` (`state.py:4113-4119`), with
`unwraps_to_id_string = unwrap_layered.local_id_string()` (the
unwrapped data half). The connector reads `precomp.unwraps_to` — a
**bare** unwrap-target id_string — and interns a bare ref for it
(`connector.ts:269-271`). The `*_id_string` field matches (both are the
unwrapped data half's local string); the **ref identity differs** (bare
vs. layered).

(b) **Assessment / action.** This is a *deliberate* consequence of the
stateless design — refs on this wire are always bare, provenance never
crosses it (item 3) — not a fixable drift, since the connector has no
layered id to intern. The right move is to **document it as an accepted
divergence** (the `unwraps_to` ref is the bare unwrap target, not a
provenance-layered one) rather than change behavior. Flagged here so it
isn't mistaken for a bug during review. Same reasoning applies to
`context_data` / `backstep` (bare refs from precomp).

### 5. Prefix-length + non-prefixable handling must port with item 1

(a) **Observable difference.** Independent of the hash-vs-prefix issue,
the connector has no notion of the workstation's
prefixable/non-prefixable tag split or its prefix-length default — it
truncates every hash to 6 chars. After fixing item 1, the connector
must reproduce both: prefix length 7 for `d`/`c`/`n`, full id_string for
`.`/`!`/`p`/`u`.

(b) **To make it consistent.** Fold this into item 1's allocator (it's
the same code path); called out separately only so the
non-prefixable-tag case isn't forgotten when the prefix policy is
ported. The collision-extension loop already present in `allocate`
covers the "extend one char on collision" requirement once it slices
from `idString`.

**Fixed (2026-06-14):** folded into item 1 — `allocateEntityShortId`
branches on `PREFIXABLE_TAGS` and falls back to the full `id_string`
for non-prefixable tags; `ENTITY_PREFIX_LEN = 7`.

### Out of scope / confirmed-consistent

- **Descriptor canonicalization** (`descriptor.ts:canonicalizeDescriptor`):
  drops `service_type_id`, resolves URLs, sorts keys — matches Q-J and
  the "same identity ⇒ same short ID" intent. No change.
- **Service metadata** (`metadata.ts:buildServiceMetadata`): reads
  `type_id_string` off the descriptor's `service_type_id`, matching
  `state.py:_compute_service_metadata` (`type_id.id_string()`). No
  change.
- **`classifyShortId` answered from the table** rather than by parsing
  the short ID: matches the workstation's list-scan classifier
  (`ShellRefStubResolver.tsx`) in *approach*; the short-ID *format*
  fixes above are about display/debuggability, not classification.
- **Invoke dispatch by descriptor kind** (`session.ts:dispatchInvoke`,
  http/ws/transient) and the `subscribe_to` desugar: covered by the
  behavioral audit below, not the short-ID/metadata audit.

## Consistency audit — invoke / load / callback behavior

A second read-through (2026-06-14), broader than identifiers: does the
client-local session reducer drive the **abstract stub surface**
(`OpResultStub` `result`/`committed`/`done`; `CallbackInboxStub`
`events`/`state`/`connectionState`/`subscriptionBreakReason`/`offset`)
the same way `WorkstationSession` does, so the *same frontend code*
observes the same behavior against either connector? The bar is
**observable consistency of the stub atoms**, not identical internals —
modulo the workstation's extra failure/rollback states (mutation
reject, `stale`, server-side validation) that the stateless connector
structurally cannot produce. Compared `session.ts` / `stubs.ts` against
`connector-workstation/src/session.ts` + `stubs.ts` and the
`callback_impl.py` / `client_protocol.py` server semantics. Two real
inconsistencies (I1, I2), two narrower divergences (I3, I4), and a
confirmed-consistent list. **Findings I1 + I2 were reproduced with a
throwaway probe test before write-up** (cross-tick load minted slots 1
then 2; `subscribe_to` filled with a `ServiceRefStub`, not `null`).

### I1. Load / metadata dedup is lost across flushes (re-fetches; new slot)

(a) **Observable difference.** On the workstation, `allocateLoad` (and
the two metadata allocators) dedup against a **wire-visible, committed**
`dedup` map (`local.dedup[kind][shortId]`), so loading the same entity
in *any later tick* (or after a foreign mutator, or after re-attach)
returns the **same already-filled slot** — the stub's `result` is
immediately `filled`, no second fetch. The HTTP session keeps its
`committedDedup` only as an in-memory field that it **rebuilds from
scratch every flush, populated solely from that flush's pending specs**
(`session.ts:flush` → `forwards`, `:373`, then `committedDedup =
forwards`, `:420`). There is no committed slot→(kind,shortId) reverse
index, so a committed dedup entry survives exactly **one** flush and is
then dropped. Net effect, confirmed by probe: load `E` (slot 1, fills) →
any intervening unrelated write/flush → load `E` again ⇒ **MISS ⇒ new
slot 2 + a second `/data` fetch**, where the workstation returns slot 1
filled. Same frontend code, different observed `result` (a fresh
`pending` stub vs. the cached `filled` one) and a redundant network
fetch. Same-tick dedup is unaffected (it rides `pendingDedup`, which is
why the existing `session.test.ts` "dedups within an updater" passes and
the gap went unnoticed).

(b) **To make it consistent.** Make committed dedup persist like the
workstation's: keep a long-lived `slot → {kind, shortId}` reverse index
(populated at allocate, mirroring `committedSlotDedupInfo`), and during
the flush walk repopulate `forwards` for **committed** slots from it (as
`WorkstationSession.flushWalk` does), not only for slots with a pending
spec. GC entries in `reindexCommittedState` when the slot leaves
`committedSlotSet` (and isn't pending). Equivalent alternative: stop
resetting `committedDedup` each flush — add committed entries and only
remove them on GC. Add a cross-tick dedup test (the current suite only
covers same-tick).

**Fixed (2026-06-14):** added a long-lived `dedupSlotInfo: Map<slot,
{kind, shortId}>` (populated at allocate, GC'd in
`reindexCommittedState` when the slot leaves committed). The flush walk
now repopulates `forwards` for committed dedup slots from it, so
cross-tick / cross-flush dedup persists exactly as long as the stub
stays in committed state (and drops once it leaves, matching the
workstation). Regression test added (`session.test.ts`: same entity
loaded across ticks reuses the slot with no second `/data` fetch; a
fresh load after the stub leaves committed refetches).

### I2. `subscribe_to` invoke result is the unsubscribe handle, not `null`

(a) **Observable difference.** The uniform API is
`ctx.allocateInvoke(inbox.handle, "subscribe_to", [source])`, returning
an `OpResultStub`. On the workstation this is a `CallbackInvokeOp` on
the inbox's own `WorkstationSessionEventInbox.subscribe_to`, whose
declared return is **`-> None`** (`protocols.py`), so the op-result fills
with **`value: null`**. The HTTP session reinterprets the handle-invoke
as `source.<method>(handle)` and fills the slot with **whatever the
source returned** — for a ws source that's the **transient unsubscribe
handle** (a `ServiceRefStub`); confirmed by probe (`value:
<ServiceRefStub s1>`). So the same `sub.result` reads `filled · null` on
the workstation and `filled · ServiceRefStub` on HTTP. The connector
*already* captures that handle internally for best-effort unsubscribe
(`fireSubscribeTo` → `st.unsubscribe`), so leaking it as the user-facing
result is both inconsistent and redundant.

(b) **To make it consistent.** Have `fireSubscribeTo` keep recording the
unsubscribe handle + `(url, generation)` internally but **return `null`**
(→ `fill(slot, {value: null})`), matching the workstation's
`subscribe_to -> None`. The `connectionState`/break machinery is
unchanged; only the op-result payload changes.

**Fixed (2026-06-14):** `fireSubscribeTo` now returns `null` after
recording the unsubscribe handle internally. Regression test added
(`session.test.ts`: a ws `subscribe_to` fills the op-result with
`{status: filled, value: null}` while still transitioning the inbox to
`connected`).

### I3. `connectionState` stays `idle` for non-ws (GET) sources

(a) **Observable difference.** On the workstation, a *successful*
`subscribe_to` against **any** source emits `CallbackSubscriptionEvent(connected=True)`
ahead of the invoke result, so `inbox.connectionState` flips
`idle → connected` regardless of source kind. The HTTP session flips
`connected` only for `ws`/`transient` sources with a live generation
(`fireSubscribeTo`, `:629-640`); for an `http` (GET) source it stays
`idle` forever (no live channel exists). For a **ws-backed** stateless
source — the apples-to-apples case — the behavior matches
(`connected`, then `broken` on drop). The divergence is only for a GET
source, which genuinely cannot hold a live subscription.

(b) **Assessment / action.** Largely **inherent** (a GET service has no
push channel — the overview's "a GET-backed service just never emits
events" case), so frontend reload-on-`broken` logic correctly never
fires for it. But it *is* observably different if the *same logical
subscribable source* is modeled as RPC on the workstation and as GET on
the stateless server. Documented as an accepted degenerate case rather
than synthesizing a fake `connected` the transport can't back.

**Documented (2026-06-14):** the `CallbackInboxStub.connectionState`
docstring in `@fxtr-ui/core` now states that subscribing to a
non-pushing source (a service that statically has no events, or an
`http`/GET stateless service) leaves `connectionState` `idle` forever —
uniform across both connectors. No behavior change; a regression test
(`session.test.ts`: `subscribe_to` an http source → result `null`,
`connectionState` stays `idle`, no ws opened) locks it in.

### I4. Notify can append to an inbox before its slot commits

(a) **Observable difference.** `allocateCallback` registers the inbox's
notify route **immediately at allocate time** (`session.ts:522`), and
`handleNotify` appends to `inboxLogs` unconditionally — so an event that
arrives in the microtask window before the allocating flush commits is
visible on `inbox.events` while `inbox.state` is still `"opening"`. On
the workstation the inbox can't deliver before the `CreateCallbackOp`
commits server-side, so `events` is non-empty only once `state ===
"open"`. The part already notes this as a deliberate relaxation of the
"committed-first" invariant (step G, `connectionState` note).

(b) **Assessment / action.** Narrow (a sub-microtask window) and
deliberate, but easy to remove — a notify cannot actually arrive until
the `subscribe_to` op *fires*, which is already post-commit, so route
registration had no reason to be at allocate time.

**Fixed (2026-06-14):** `allocateCallback` no longer registers the
notify route; `flush` registers it for newly-committed callback slots
(before firing ops, so a `subscribe_to` fired in the same flush still
has a live route). Restores the committed-first invariant and means an
allocated-but-never-stored callback opens no route at all. `InboxState`
gained a `registered` flag; the happy-path callback test still passes
(subscribe→notify routing now depends on commit-time registration).

### Confirmed-consistent (no change)

- **`OpResultStub` state machine** — `result` precedence (broken →
  errored, then `slotFills`: absent ⇒ `pending`, errored ⇒ `errored`,
  else `filled`), `committed` = slot ∈ committed set, `done` = result ≠
  pending, and the `slot=0` dead sentinel (errored/`done`) all mirror
  the workstation stub. The observable **sequence** (`pending →
  filled/errored`, `committed` flips once) is identical; only wall-clock
  timing differs (commit at the flush microtask vs. after a server ack —
  inherent, not a divergence).
- **Errors are fills, never rollbacks** — a failed fetch / dropped ws /
  stale transient surfaces as an `errored` fill on the affected slot
  (`fireOp`'s catch), exactly like a workstation `invoke_error`;
  mutations always commit locally. Matches the governing principle.
- **`allocateInvoke` semantics** — fresh slot per call (no dedup), args
  encoded at allocate time (fail-fast on bad arg / cross-session
  handle), op fires only if the stub survives into committed state
  (allocate-and-discard never fires). Matches `WorkstationSession`.
- **Callback `events` / `sliced` / `offset`** — per-slot log sliced from
  `offset - firstIndex`, `sliced(n)` mints a forward cursor, cursor
  advance trims the front and a removed inbox drops the log in
  `reindexCommittedState`. Mirrors the workstation inbox stub.
- **Break-on-drop** — a ws generation drop flips the matching inbox
  `connectionState → broken` with a reason and breaks transient stubs
  (`onConnectionDrop` + connector `markServiceBroken`), the local
  synthesis of the workstation's `CallbackSubscriptionEvent(connected=False)`
  / `ServiceBrokenNotification`. Consistent.
- **Cross-session stub substitution + GC** — foreign op-result / inbox
  stubs collapse to dead sentinels at flush; `slotFills` / `inboxLogs`
  GC when a slot leaves committed state. Matches the workstation.
