# 04 — Client connector (`@fxtr-ui/connector-http`)

Status: resolved. The connector is the transport + identity layer;
sessions (05) hang off it.

## Re-orientation pointers

| What | Where |
|---|---|
| Workstation connector (the shape we mirror) | `fxtr-ui-connector-workstation/src/connector.ts` — intern maps `:162-163`, `entityRef`/`serviceRef` `:337-355`, `loadEntityData` `:309-327`, `createSession`/`attachSession` `:220-264`, lists `:191-205` |
| ReactiveList interface | `fxtr-ui-connector-workstation/src/resource_list.ts:57-60` (`status`, `records`); lifecycle `:98-211` |
| Workstation reconnect machinery (contrast, mostly *not* copied) | `fxtr-ui-connector-workstation/src/connection.ts:206-211` (backoff schedule), `:486-494` (scheduleReconnect), `:252` (outbound queue) |
| Shell layout persistence (storage-coupling constraint) | `fxtr-ui-workstation/src/persistence/layout_v6.ts:44-122` (localStorage key `fxtr-workstation-layout`, embeds **sessionIds**), `persistence/dispatcher_session_id.ts` |

## Construction

```ts
new HttpConnector({
  baseUrl: string,
  storage: Storage,              // the standard Web Storage interface
  fetchFn?: FetchFn,             // tests / node
  webSocketCtor?: typeof WebSocket,
  store?: JotaiStore,
})
```

Owns: jotai `store`; the shortId table; entity/service stub intern
maps (same shape as `connector.ts:162-163`); the `HttpReadableArena`
(03); the ws pool; the discovery lists; the session registry.

## Storage — the standard `Storage` interface

We **do not invent a storage primitive**: the connector accepts the
built-in Web Storage interface (`lib.dom.d.ts` `Storage`:
`getItem` / `setItem` / `removeItem` / `key` / `length` / `clear`).
`localStorage` and `sessionStorage` are passed in directly — no
adapter, no `webStorage()` wrapper. The only thing with no standard
built-in is an in-memory store, so `@fxtr-ui/core` provides a ~6-line
`memoryStorage(): Storage` (a `Map` behind the three methods) for
tests / SSR. The param type can narrow to
`Pick<Storage, "getItem" | "setItem" | "removeItem">` since that's all
we use — enumeration is **not** needed (see key schema). `sessionStorage`
is the default; `localStorage` is opt-in (documented last-writer-wins
across tabs, Q-10).

This `Storage` handle is the same one the PanelViewer takes for layout
persistence (design/06). **In the HTTP app you pass one handle to
both** the connector and the PanelViewer (open_questions #8) so the
connector's sessions and the layout that embeds their sessionIds age
together — Part 4 changes `layout_v6.ts` / `dispatcher_session_id.ts`
to take a handle instead of hardcoding `localStorage`. (The workstation
app passes a handle to the PanelViewer only; its connector persists
nothing.)

Key schema (namespaced so two backends on one origin don't collide):

```
fxtrhttp:<ns>:table                  # shortId table (one blob or chunked — TODO)
fxtrhttp:<ns>:sessions               # registry: [{sessionId, metadata, createdAt}]
fxtrhttp:<ns>:session:<uuid>         # one session's persisted state (05)
```

Enumeration uses the **`:sessions` registry blob** (and the known
`:table` / layout keys), never a storage-key scan — which is why we
need no `list(prefix)` beyond the standard `Storage` surface.
`<ns>` defaults to a hash of `baseUrl`; overridable.
Persist-failure handling (quota / disabled storage, resolved Q-G):
**loud `console.error`, keep going with in-memory state
authoritative** (matching the codebase's "warn loudly, don't crash"
pattern, cf. the `validation_failed` canary in `session.ts`).

## ShortId table

Persisted map `shortId → {kind: "entity", idString} | {kind:
"service", descriptor}` plus reverse indexes (in-memory). Allocation
mirrors the workstation's `short_ids.py`: an entity shortId is a
*prefix of the data `id_string`* (prefixable `d`/`c`/`n` tags → first
7 chars, extended one char at a time on collision; non-prefixable
`.`/`!`/`p`/`u` tags → the full `id_string`); a service shortId is an
ascending `s{N}` per canonical descriptor, its high-water mark
persisted so post-reload allocations keep ascending. A *layered*
entity is keyed by its `(data, prov)` pair and gets the workstation's
provenance-suffix forms (`@N`/`@0`/`@u` for prefixable buckets, a
composite `@{prov-shortId}` for non-prefixable) — the full
`short_ids.py` allocation is ported, with the `@N` high-water rebuilt
from the persisted entries. Loaded at
construction; appended-to as new
identities are seen (decode, discovery, loads); written through the
shared storage handle. Transient descriptors get table entries too
(they appear in persisted session state) and rehydrate born-broken.

The table also backs the connector's `classifyShortId(shortId)`
(design/06): the entry's `kind` answers entity-vs-service
authoritatively — no reliance on shortId prefix syntax, and no need
for the (dropped) portable entity/service lists. An unknown shortId
returns `null` (the resolver renders it as unresolved text).

## Discovery + lists

`GET /discovery` once at construction; `listEntities()` /
`listServices()` return `ReactiveList`s (same interface as
`resource_list.ts:57-60`) whose records derive from the snapshot:
status `loading → ready`, plus a connector-level `refresh():
Promise<void>` that refetches and republishes (no live deltas —
nothing to push). Multiple discovery names mapping to one identity
group into a single record with `external_names: string[]` (matching
the workstation record convention); service records also carry
`service_type_id` straight from discovery (design/01). These lists
are **`HttpConnector`-specific** — consumed by the connector's *own*
roots panel, not by a portable interface (design/06): after the
PanelViewer refactor (Part 4) there is no shared
`listEntities` / `listServices` / `UiEntityRecord` /
`UiServiceRecord`.

## ws pool

One `WsConnection` per distinct ws `url`, holding: socket, monotonic
**generation** counter, per-connection pending-invoke map, and the
set of transient stubs minted on the current generation.

**Connect-on-demand, no background reconnect loop** (resolved Q-A): a
connection is opened (or reopened) when an invoke needs it; invokes
arriving while opening wait for that attempt; if the attempt fails
they fill as `invoke_error`. On drop: pending invokes error,
generation bumps, transient stubs of the old generation break, and
inboxes that held a live subscription over that connection flip to
`connectionState: "broken"` (design/05 §Inboxes); nothing reconnects
until next use. Rationale: the governing principle says breakage is
per-service error surfacing, and the workstation does not
background-retry bridges to crashed peers; this also avoids
reconnect-timer state entirely. (The workstation-connector style
backoff loop, `connection.ts:486-494`, stays available as a fallback
if on-demand ever feels laggy in practice.)

`connectionStatus: Atom<ConnectionStatus>` is derived: `"open"` if
all pool connections that exist are open (or pool empty),
`"connecting"`/`"reconnecting"` while an attempt is in flight,
`"closed"` after `connector.close()`. Sessions do **not** consume
this (their lifecycle is local; open_questions #12) — it exists for
the shell header.

## Sessions

`createSession(metadata)` mints a UUID, writes the registry +
initial blob, returns the live session (sync in nature; kept
`Promise`-returning to match the abstract surface).
`attachSession(sessionId)` rehydrates from storage (unknown id →
same error shape as workstation's attach-to-missing).

**Deviation from the workstation connector** (resolved Q-B —
documented as a deliberate difference): workstation `attachSession`
returns a *fresh independent instance per call* (`connector.ts:255-264`),
which works because the server arbitrates. Here two live instances of
the same session in one connector would race on the same storage key,
so we **intern one live session instance per sessionId per
connector** — `attachSession` returns the existing instance if alive.
Cross-tab (localStorage) remains last-writer-wins. This difference
must be called out in the connector's docs/CODE_OVERVIEW so consumers
don't assume per-call independence.

`loadEntityData(stub)`: direct load through the arena (03); unlike
the workstation's, children are fully loadable (no inactive-shortId
caveat).

`close()`: closes pool connections, flushes pending persists, marks
lists closed, detaches live sessions. Idempotent, mirroring
`connector.ts` close semantics.
