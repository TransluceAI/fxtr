# Part 7a — design scratch (concise)

One entry per topic thought through so far: the conclusion and the
one-paragraph why. Full reasoning, alternatives, and pros/cons live in
[`design_scratch_details.md`](design_scratch_details.md).

## 1. Persistence format: workstation wire form (short-ID tags)

_(Revised per Daniel's note — stable-identifier blobs added
complexity for no benefit once the table is persisted anyway.)_
Persist userState (and inbox logs, invoke fills) in the same
list-escape JSON form the workstation server persists: `["entity",
shortId]`, `["service", shortId]`, `["op_result", slot]`, `["inbox",
slot, offset]`. The persisted shortId table (§7) supplies shortId →
id_string/descriptor resolution; table + blobs live in the same
storage backend so they can't diverge. The *server* wire still uses
stable identifiers (the server can't know shortIds) — the codec has
a shortId mode (persistence, identical to today's `escapeWire`) and
a resolved mode (server wire), with the connector's table translating
between them. See design/01 + design/05.

## 2. Storage: thin hand-rolled interface, no state library

Surveyed jotai `atomWithStorage`, zustand/persist, dexie/idb-keyval.
None help much: our state already lives in jotai atoms driven by a
custom reducer with its own serialization (stubs), and the write path
must stay "reducer commits → persist snapshot", not "library syncs
atom". Conclusion: persist through a thin synchronous string-keyed
store. **Revised (final):** don't invent the interface — use the
standard Web Storage `Storage` (`localStorage`/`sessionStorage` pass in
directly); the only hand-rolled piece is a ~6-line `memoryStorage()`
for tests, in `@fxtr-ui/core`. Enumeration goes through the `:sessions`
registry blob, so no `list(prefix)` is needed. See design/04 §Storage.
IndexedDB (idb-keyval) only if payload sizes ever force it — they
shouldn't, since entity payloads are not persisted.

## 3. Reducer: fork-and-simplify, not parametrize

The HTTP session reuses the workstation session's *concepts* (slot
counter, stub interning, dedup maps, inbox logs/offsets, flush-walk
collect) but drops everything that exists only for the server
protocol: JSON-patch diffing, ack seqs, preserve/unpreserve, foreign-
mutator reconciliation, stale-event handling, cross-session-stub
substitution (still needed actually — kept), mutation queue/rollback.
Writes commit synchronously to the local atom, then persist. Trying
to parametrize `WorkstationSession` over "is there a server" would
thread conditionals through its hardest code; forking is safer and
the fork is much smaller than the original.

## 4. Entity loads: decode via the existing TS arena client

`allocateLoad` fetches `/data/{id}` and decodes with the machinery
from `v2/ts/packages/fxtr-http-client` (`HttpReadableArena` ingests
bytes + precomp). The connector layers a small adapter that walks the
decoded entity into `ResultValue` shape — child refs (tags
33001/33002) become interned `EntityRefStub`s, `$fxtr.type` surfaces
the same way the workstation load path surfaces it. Metadata ops read
the precomp closure (unwraps_to / ctx_node / backstep) instead of a
server round trip. Parity target: a renderer must not be able to tell
which connector loaded its entity.

## 5. ws RPC: four frame types, connection-scoped routing only

_(Revised per Daniel's notes: transient stubs added; no replay; no
mutation-level rejection.)_ `invoke` (client→server, carries id +
service name or transient id + method + escaped args/kwargs),
`result` / `error` (terminal, reference the request id), `notify`
(server→client, carries inbox UUID + escaped payload). Server keeps
one **per-connection table**: inbox UUIDs seen in callback tags (for
notify routing) and **connection-scoped transient services** the
server handed out (`["service", {kind: "transient", id}]`) — e.g.
the unsubscribe handle returned by `subscribe`. Client-side, a
transient stub is bound to the (url, connection-generation) it
arrived on: invokes route there; escaping it toward any other
destination is a runtime error; on disconnect or reload it breaks
(all calls error). On a dropped connection: in-flight invokes fill
as ordinary `invoke_error` events (never mutation rejections),
transient stubs break, inbox routing dies, and **nothing is
replayed** — same as a workstation losing an attached service.
No request timeouts; ordering is the server's business; optional
framework-level ping. Details in design/01.

## 6. `subscribe_to` desugars to a `subscribe` invoke on the source

The workstation implements inbox `subscribe_to(source)` server-side;
here the inbox is client-local, so `subscribe_to` becomes: invoke
`subscribe(handle)` on the source service, with the callback tag
riding as an arg. ws source → server routes notifies to the inbox
and returns a transient unsubscribe service. http source → server
ignores the callback arg, returns whatever (possibly null), and no
events ever arrive — exactly the agreed degradation story for
dynamic-dispatch hot reload; the dispatcher code doesn't change at
all (confirmed: it never reads `subscribe_to`'s return value,
`dynamic_dispatcher.ts:528-535`). **No subscription replay on
reconnect** — a dropped connection is an attached-service crash, and
a workstation wouldn't re-subscribe on the frontend's behalf;
uniform breakage detection + resubscribe is the separate precursor
change (broken grants → broken unsubscribe handles), see overview.md
"Precursor work".

## 7. Short IDs: persist the allocator table at connector scope

A truncated-hash scheme cannot *guarantee* uniqueness, and the
collision-extension needed to fix that is history-dependent — so pure
derivation fails exactly when collisions happen. Decision: persist
the shortId ↔ stable-identifier table in connector-scope storage,
**in the same storage backend as the sessions and the shell's layout
persistence** (no durable/non-durable split — Daniel's Q8 response;
the shell already handles "session gone", and all three pieces must
age together). With persistence format §1 now short-ID-based, the
table is load-bearing for rehydration, which is fine since it can
never be separated from the blobs it serves. Allocation still uses
the workstation-style hash-prefix policy (entities: `d` + data-hash
prefix; services: `s` + descriptor-hash prefix) for debuggability
and syntactic `classifyShortId`. **Correction (2026-06-14):** the hash
policy was an inconsistency with the real `short_ids.py` — entities now
use a *prefix of the `id_string`* and services an ascending `s{N}`; see
the consistency audit in `part3_connector.md`. Note: the shell's layout blob
embeds *sessionIds* (`persistence/layout_v6.ts`), not shortIds —
shortIds live inside session userState — which is exactly why
layout, sessions, and table must share one storage backend.

## 8. Shell connector interface: ~8 members, feature-detected extras

Sketch (final scoping in part 2):

```ts
interface UiConnector {
  store: JotaiStore;
  url: string;                         // display label for status header
  connectionStatus: Atom<ConnectionStatus>;
  entityRef<T>(shortId): EntityRefStub<T>;
  serviceRef<T>(shortId): ServiceRefStub<T>;
  listEntities(): ReactiveList<UiEntityRecord>;
  listServices(): ReactiveList<UiServiceRecord>;
  createSession(metadata): Promise<Session>;
  attachSession(id): Session;
  loadEntityData<T>(stub): Promise<T>;
  close(): void;
}
```

`UiEntityRecord` / `UiServiceRecord` are the common subset;
likely home: `@fxtr-ui/core` (it already owns the abstract Session);
`ReactiveList` moves there too.

_(Re Daniel's TODO on what the records actually are, and his
"feature-detect doesn't seem clean" on debug panels: both now have
concrete proposals + alternatives in
[design/06](design/06_shell_decoupling.md) — record shapes grounded
in the shell's actual field accesses, and four debug-panel options
with a recommendation for injected panel factories. Open questions
C + D.)_

## 9. Server-side service helpers: function-per-service + escape ctx

Backend authoring shape (both languages): a service is an async
function `(method, args, kwargs, ctx) -> result` (or a small
method-map); the library wraps it into a Starlette route / Hono
handler, doing unescape (entity tags resolved against the scope →
FxtrID; service/callback tags → descriptor/UUID values) and escape of
the result (FxtrID → entity tag, descriptor passthrough). The ws
endpoint helper owns the read loop, request dispatch, and
`ctx.notify(inbox_uuid, payload)` which routes to the connection that
sent the inbox. Kept deliberately small; duplicating ~200 lines
between py and ts is fine.
