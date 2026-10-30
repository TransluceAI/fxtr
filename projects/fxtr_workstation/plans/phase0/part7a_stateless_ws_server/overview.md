# Part 7a — Stateless backend + client-state HTTP connector: overview

Status: **open questions resolved (2026-06-13).** This is the
high-level overview; the per-piece detailed design lives in
[`design/`](design/) and the implementation plans in the part docs
(`part1_dispatcher_reload.md` / `part2_backend.md` /
`part3_connector.md` / `part4_panelviewer.md` / `part5_demo.md`).

Companion docs: [`open_questions.md`](open_questions.md) (the decision
record), [`sketch.md`](sketch.md) (original sketch),
[`design_scratch.md`](design_scratch.md) (concise record of what was
thought through and why; long form in
[`design_scratch_details.md`](design_scratch_details.md)).

## Goal

Run the existing workstation frontend UI (shell, dispatch, renderers)
over a **stateless, CDN-friendly backend**. All session state lives on
the client in pluggable local storage; the server is a pure
request→response function plus transient per-WebSocket-connection
subscriber routing. In exchange the backend becomes drastically
simpler than the full fxtr-RPC workstation: a Starlette (or, later,
Hono) app with a handful of endpoints, without the fxtr RPC system.
The design is language-agnostic and implementable twice; **this change
builds the Python backend, and the TypeScript backend is deferred to a
follow-up** (see "Backend libraries").

This realizes (and substantially extends) the long-standing
`fxtr-ui-connector-http` TODO from
[`part3_frontend_ui/design_notes/02_connector_abstraction.md`](../part3_frontend_ui/design_notes/02_connector_abstraction.md):
the original sketch there was a read-only degenerate session; this
design adds client-stateful sessions that can also hold **services**
(invokes, callbacks), which the read-only design could not.

## Governing principle: a workstation in the browser

Conceptually, the client connector **emulates a workstation server
running in the browser**, into which the remote server has bridged a
set of external services and an entity store via the discovery
endpoint. Every semantic question should be answered by asking *what
would happen on a real workstation if the corresponding attached
service disconnected or broke?* Concretely:

- **Mutations always commit locally.** The connector never rejects a
  mutation because something remote broke (only for malformed input,
  which throws at allocate/encode time). Remote breakage surfaces as
  ordinary error events on the affected slots.
- **Disconnections are per-service breakage, not session failure.**
  A dropped ws connection is the analogue of an attached RPC peer
  crashing: in-flight invokes on it fill as `invoke_error`,
  connection-scoped services break, inbox deliveries stop. The
  session itself stays healthy.
- **No transparent retry or replay** of anything the workstation
  server wouldn't be allowed to retry — in particular no automatic
  re-sending of invokes and no replaying of subscriptions after
  reconnect. Frontend code is expected to detect breakage and
  recover, exactly as it must against a real workstation (see
  "Precursor work" below).

## What inverts relative to the workstation

| Aspect | Workstation (`/client`) | Stateless backend |
|---|---|---|
| Session state | server-side `SessionRecord`, JSON-Patch mutations, acks | client-side only; no wire mutations, no acks |
| Short IDs | server-allocated, server-lifetime | client-allocated, presentation-only |
| Entity identity on wire | server short_id | full `id_string` (content-addressed) |
| Service identity on wire | server short_id | self-describing descriptor (URL-based) |
| Refcounts / track ops | server pins per session | none; track/untrack are client no-ops |
| Result delivery | session event log + retention | client fills slots directly from responses |
| Callbacks | workstation-minted inbox services | client-local inboxes with client-minted UUIDs; ws routing only |
| Lifetime of returned services | scoped, refcounted | assumed stateless + arbitrarily long-lived |

## Identifiers on the wire

- **Entities** — full `id_string` (the existing `FxtrID.id_string()`
  format). Escape tag: `["entity", "<id_string>"]`. Any entity the
  client sends must already be known to the server (the server has no
  way to fetch client data).
- **Services** — self-describing descriptor union, escape tag
  `["service", <descriptor>]`:
  - simple: `{kind: "http", url, method: "GET" | "POST"}`
  - event-stream: `{kind: "ws", url, name}` (all services sharing a
    ws `url` multiplex over one connection)
  - Every descriptor may also carry a **`service_type_id`**
    annotation (the type entity's `id_string`). The server includes
    it wherever it introduces a service — discovery entries and
    descriptors returned from invokes — mirroring the workstation,
    which knows a service's type eagerly at registration. It is a
    *non-identity* annotation: descriptor canonicalization for shortId
    hashing **excludes** it. The connector reads it to populate
    records and to answer `allocateGetServiceMetadata`
    (`type_id_string`) locally without an invoke. Dynamic dispatch is
    unaffected — it still discovers a service's *loadable* type entity
    via the `$fxtr.type` invoke, which is a different thing from the
    string.
  - **connection-scoped transient**: `{kind: "transient", id}` — a
    service minted by a ws service *for this connection* (e.g. an
    unsubscribe handle returned by `subscribe`). It lives in the
    server's per-connection table and dies with the connection. The
    client binds the stub to the (url, connection-generation) it
    arrived on: invokes route over that same connection; sending it
    as an arg anywhere else is a runtime error; when the connection
    drops (or after a reload) the stub is **broken** and all calls
    error. The workstation analogue: a service registered by an RPC
    peer that crashed.
- **Callbacks** — `["callback", "<inbox_uuid>"]`, a client-minted
  UUID identifying one of the client session's own inbox services.
  Callback args may be sent to **any** service; a plain HTTP service
  simply has no channel back and ignores them, while a ws service
  registers the inbox UUID in its per-connection routing table so
  notify frames can route back. This means client code (e.g. the
  dynamic dispatcher's reload subscribe) works unchanged against
  either kind — a GET-backed service just never emits events, and can
  later be swapped to a ws service with no client change.
- **Short IDs** are purely client-side: the connector keeps a
  persisted shortId ↔ stable-identifier table and presents
  workstation-style short IDs to the UI layer. Nothing short-ID-
  shaped crosses the *server* wire. The table lives in the **same
  storage backend as the sessions** (and as the shell's persisted
  layout) and is load-bearing for rehydration: persisted session
  state uses the workstation wire form (`["entity", shortId]` etc.),
  exactly like the workstation server persists session state, and
  the table maps shortIds back to id_strings / descriptors.
  Allocation mirrors the workstation's `short_ids.py`: an entity
  shortId is a prefix of the entity's own `id_string` (prefixable
  `d`/`c`/`n` tags → first 7 chars, extended on collision;
  non-prefixable tags → the full `id_string`), and a service shortId is
  an ascending `s{N}` per canonical descriptor — so the IDs stay
  consistent with the real identities and `classifyShortId`-compatible.

The tag-escape codec is the same list-based escape family as the
workstation wire (`wire_escape.py` / `escape.ts`), with the identifier
payloads swapped as above.

## Server surface

1. **Entity data endpoints** — the existing experimental HTTP API,
   unchanged in design: `GET /data/{id_string}` (CBOR bytes + precomp
   closure), `GET /pin/{name}`, `GET /pins`, `GET /preview/{id_string}`.
   Already implemented in both Python
   (`v2/fxtr2/experimental/http/asgi_app.py`, Starlette) and TS
   (`v2/ts/packages/fxtr-http-server`, Hono).
2. **Discovery endpoint** — one JSON GET (path TBD, e.g.
   `/discovery`) returning the named things this server exposes:
   `{entities: {name: id_string}, services: {name: descriptor}}`.
   **The library has no opinion about discovery content** — what to
   list is entirely the server author's choice (the demo will simply
   mirror its pins); the library just provides the response shape
   and a small registry helper so discovery can't drift from the
   actually-mounted routes. Plumbing type-id-strings through
   discovery (to skip the `$fxtr.type` round trip for service
   dispatch) is a deliberate **later optimization**; the baseline is
   the method convention below.
3. **Simple service endpoints** — one route per service, fixed at
   server construction. Invocation encoding:
   - `GET`: `?method=<name>&payload=<urlencoded JSON {args, kwargs}>`
     (CDN-cacheable for deterministic services).
   - `POST`: JSON body `{method, args, kwargs}`.
   - Response: tag-escaped JSON result; errors are non-2xx with a JSON
     error body. Results may contain entity id_strings (statelessly
     fetchable from `/data`) and new service descriptors (usually
     URLs on the same server, undiscoverable but valid).
   - Services that want to participate in dynamic dispatch implement
     the `$fxtr.type` method (returns their type entity ref), same as
     workstation services.
4. **WebSocket RPC endpoint(s)** — a simple JSON RPC, structurally
   similar to the workstation client protocol but much smaller.
   Sketch (details in [`design/01_wire_protocol.md`](design/01_wire_protocol.md)):
   - client→server: `{type: "invoke", id, service, method, args, kwargs}`
     (`service` is the descriptor's `name` or a transient id; the URL
     is the connection).
   - server→client: `{type: "notify", inbox, payload}` (subscriber
     deliveries, routed by the inbox UUID the client supplied),
     `{type: "result", id, value}`, `{type: "error", id, message}`
     (terminal, referencing the original request `id`).
   - The only server-side state is the **per-connection table**:
     inbox UUIDs seen in callback args (for notify routing) and
     transient services the server has handed out. Both die with the
     connection.
   - No request timeouts; ordering is the server's business (same as
     the server-session case); pings optional at the framework
     level. On a dropped connection, in-flight invokes fill as
     `invoke_error` events — never mutation rejections — and
     transient stubs break, per the governing principle.

## Client connector (`@fxtr-ui/connector-http`)

**Connector** — constructed from a base URL (+ fetch fn override for
tests). Owns: a discovery snapshot backing `listEntities()` /
`listServices()` (fetch-on-connect `ReactiveList`s with a manual
`refresh()`; no live deltas), a ws connection pool (one per ws URL,
auto-reconnect), the shortId intern tables, `entityRef`/`serviceRef`,
`loadEntityData`, and `createSession` / `attachSession`.

**Session** — implements the abstract `Session` from `@fxtr-ui/core`
(`sessionId`, `lifecycle`, `userState: FxtrSessionAtom`, `detach()`)
including the ctx-style write surface (`allocateLoad` /
`allocateInvoke` / `allocateCallback` / `allocateGetEntityMetadata` /
`allocateGetServiceMetadata` via `FxtrSessionUpdateCtx`). Session IDs
are client-minted UUIDs; `attachSession` rehydrates from storage.
The reducer is **local**: no JSON patches, no acks, no preserve ops —
writes commit immediately; `onRollback` only fires for client-side
validation failures. The slot machinery (slot counter, stub
interning, load/metadata dedup, inbox logs/offsets) is retained
conceptually from the workstation session, minus everything that
exists only because of the server protocol.

- `allocateLoad` → `GET /data/{id_string}`, decode entity CBOR
  client-side into the same `ResultValue` shape the workstation
  produces (stubs at ref leaves, `$fxtr.type` surfaced identically).
- `allocateInvoke` → dispatch on the target descriptor: http →
  fetch; ws → RPC over the pooled connection.
- `allocateCallback` → mint a client-local inbox (UUID persisted with
  the session); events arrive as ws notify frames.
- Metadata ops resolve client-side: entity metadata from the `/data`
  precomp closure (provenance parent, unwraps_to, …); service
  metadata from the descriptor (type id via the `$fxtr.type`
  convention until discovery carries it).
- Track/untrack: structural no-ops.

**Persistence** — the connector persists through the **standard Web
Storage `Storage` interface** (no custom primitive); `localStorage` /
`sessionStorage` pass in directly, and `@fxtr-ui/core` adds a tiny
`memoryStorage()` for tests (`sessionStorage` default; `localStorage`
opt-in with documented last-writer-wins multi-tab semantics). The HTTP
app passes *one* `Storage` handle to both the connector and the
PanelViewer, so the connector short-ID table + session blobs and the
shell's layout persistence age together (the shell already handles
"session gone"; it must not see layouts that outlive their sessions'
storage). Rule:
persist everything that cannot be recovered on reconnect — session
registry + metadata, userState, invoke fills, inbox logs, slot
counter, the shortId table. Entity load payloads are *not* persisted
(content-addressed; refetched on demand — their slots rehydrate as
pending and refetch). Invokes in flight at reload rehydrate as
errored ("interrupted"). Persisted state uses the workstation wire
form (short-ID tags), with the persisted table supplying shortId →
stable-identifier resolution. Transient service stubs rehydrate
born-broken. See scratch for the storage-library survey (conclusion:
thin hand-rolled layer; no external state library).

**NOTE(daniel):** The connector should persist the short-ID table alongside
its other state. Given that, it would also be totally fine to use short IDs
in all of the other locations (the short ID table would define the mapping.)

## Frontend decoupling — the PanelViewer refactor

`@fxtr-ui/workstation` currently bakes the connector type and a fixed
set of panels (roots + three debug panels) into the dockview shell.
The right cut isn't "extract an API so the existing UI talks to a new
connector" — it's **carving at the joint** between the generic
panel-viewer mechanics and the connector/app-specific panels. The
shell's core (per-panel sessions, chain/breadcrumb/history navigation,
ref-link routing, dispatch rendering, layout persistence) is already
connector-agnostic; the coupling is confined to the orchestration
layer (a fixed panel-component map and hard-coded open-panel calls).

So we split it:

- A generic **PanelViewer** takes a **`Connector`** — the shared
  consumer-facing connector abstraction we now define in `@fxtr-ui/core`
  (sibling to the abstract `Session`): `{store, url, connectionStatus,
  entityRef, serviceRef, classifyShortId, loadEntityData, createSession,
  attachSession, close}`. Both `WorkstationConnector` and `HttpConnector`
  implement it; any future viewer frontend can consume it too. The
  PanelViewer also takes a list of **panel registrations** (id + title +
  component, optionally a per-session variant). Some registrations
  surface as sidebar buttons; others are hidden and opened
  programmatically (e.g. a session-debug panel opened from a
  workstation-debug panel).
- **Roots and debug panels become connector-specific injected
  registrations.** Each connector ships its *own* roots panel using
  its own `listEntities` / `listServices` and its own record types,
  and its own debug panels (the stateless side can offer a
  storage-inspector panel a workstation can't, and vice-versa).
- This **dissolves** `listEntities` / `listServices` /
  `UiEntityRecord` / `UiServiceRecord` from the portable interface —
  there is no shared list/record abstraction to define. The one
  ref-link consumer of the lists, `classifyShortId`, becomes a
  **connector method** instead (the HTTP connector answers it from its
  shortId table; the workstation keeps its list-backed scan) —
  `RefLink` itself only ever renders the bare shortId.

The generic PanelViewer lands in its **own package**
(`@fxtr-ui/panel-viewer`); `@fxtr-ui/workstation` becomes "workstation
panel registrations + connector" depending on it, and the HTTP demo
depends on `@fxtr-ui/panel-viewer` + its own panels (never on
`@fxtr-ui/workstation`). This is a real refactor of
`@fxtr-ui/workstation` and lands as its own part. See
[`design/06_shell_decoupling.md`](design/06_shell_decoupling.md).

## Backend libraries

**Python only for this change; the TypeScript backend is deferred to a
follow-up.** The existing TS port (`v2/ts/packages/fxtr-http-server`,
Hono) is already only a partial port, and some of the TS logic this
was going to lean on has had a change of plans — so we build out the
Python backend now and pick up TS later. The per-language design notes
below and in [`design/02`](design/02_server_backends.md) are kept but
marked deferred.

Plan of record: extend `v2/fxtr2/experimental/http/` (Python/Starlette)
with the discovery, simple-service, and ws-RPC layers. **No
rename/promotion out of `experimental/`** in this change (resolved
Q-N; deferred to avoid colliding with parallel work). Writing a backend
should feel like using Starlette plus helpers: arg/result escape codecs
(entity tags resolved against a scope), service-definition helpers
(async function → route), and the ws message-routing loop. The Python
backend ships a tester script exposing at least one of everything
(entities, GET service, POST service, ws service, callbacks,
service-returning-services); the frontend integration tests spawn it
over the wire (the `_demo_workstation.py` + `_integration_harness.ts`
pattern). With only one backend, there is no cross-backend smoke test
yet — it returns when the TS backend lands.

## Precursor work — done

When a subscription's far side breaks (a workstation's attached
service crashes; our ws connection drops), the frontend needs a
*uniform* way to notice — a signal that a service handle's grant is
broken, breaking the unsubscribe handle the subscriber holds, so
frontend code can resubscribe regardless of connector. This landed as
a self-contained change to the workstation session mechanism in
[`part6_adjustments/stage4_service_break_notifications.md`](../part6_adjustments/stage4_service_break_notifications.md)
and
[`part6_adjustments/stage4post1_subscription_break_notifications.md`](../part6_adjustments/stage4post1_subscription_break_notifications.md):
the abstract `CallbackInboxStub` now carries `connectionState` /
`subscriptionBreakReason`. Part 7a builds on it directly — the
stateless connector synthesizes those transitions locally (design/05),
and the Part 1 dispatcher reload reacts to them.

## Detailed design docs

Per-piece designs (what each piece does, how they interoperate, and
file pointers into the existing code for re-orientation) live in
[`design/`](design/):

- [`design/01_wire_protocol.md`](design/01_wire_protocol.md) —
  identifiers, descriptors, escape codec variants, HTTP + ws
  invocation encodings, error shapes.
- [`design/02_server_backends.md`](design/02_server_backends.md) —
  the Python backend library (TS deferred), registry/discovery
  helpers, ws endpoint, tester script.
- [`design/03_entity_loading.md`](design/03_entity_loading.md) —
  `/data` + precomp, the client-side CBOR→`ResultValue` decode and
  its parity contract, metadata derivation.
- [`design/04_client_connector.md`](design/04_client_connector.md) —
  connector object: discovery lists, ws pool + connection
  generations, shortId table, storage.
- [`design/05_client_session.md`](design/05_client_session.md) —
  the session reducer fork: keep/drop inventory, persistence,
  inboxes and `subscribe_to`, disconnect semantics.
- [`design/06_shell_decoupling.md`](design/06_shell_decoupling.md) —
  the PanelViewer refactor (Part 4): generic core + injected
  connector-specific panel registrations, the shared `Connector`
  abstraction (in core), packaging, and why shared lists/records are
  dropped.
- [`design/07_dynamic_dispatch.md`](design/07_dynamic_dispatch.md) —
  dynamic dispatch over this connector; the Part 1 reload-on-break +
  manual reload; WS-vs-GET dispatch and hot reload.

## Parts

The original work divides into five parts (1–5, below). **Parts 6 + 7 were
added 2026-06-20** when the publishing flow was replanned around
*provenance-through-layers*: they extend the HTTP serve side to carry + consume
provenance, the serve-side complement of
`../part7b_publishing_flow/part5_layer_provenance.md` (build side). The combined
build-and-serve demo lives in `../part7c_build_and_serve_demo/`.

Part 1 lands first (it's a self-contained, connector-agnostic setup change that
makes the WS dispatcher hot-reload "just work" later). Parts 2 (backend), 3
(connector), and 4 (PanelViewer refactor) are largely independent of each other;
Part 5 (demo) depends on 2 + 3 + 4. Parts 6/7 (provenance) come after the
provenanced layers land (7b part 5) and feed the 7c demo.

- **Part 1 — dispatcher reload** (`part1_dispatcher_reload.md`):
  dispatcher-only change in `@fxtr-ui/react-dispatch` — react to inbox
  `connectionState → broken` by wiping renderer caches +
  resubscribing, and add a manual "reload everything" action to the
  dispatcher debug panel. Connector-agnostic (also improves the
  workstation), and it's what lets a WS dispatch service drop →
  reconnect → fresh renderers. See
  [`design/07_dynamic_dispatch.md`](design/07_dynamic_dispatch.md).
- **Part 2 — backend** (`part2_backend.md`): the **Python** service
  layer over the existing entity endpoints — `service_codec` (resolved-
  mode wire codec), `service_registry` (registry/discovery carrying
  `type_id_string`), `service_rest_endpoint`, `service_ws_endpoint`
  (+ per-connection table) — plus Python unit/endpoint tests. The
  kitchen-sink tester backend is built in **Part 3** (where it's used);
  the TS backend + cross-backend smoke test are **deferred to a
  follow-up**. `experimental.http` is **not** renamed/promoted here.
- **Part 3 — connector** (`part3_connector.md`):
  `@fxtr-ui/connector-http`, storage backends + shortId table, ws pool
  (connect-on-demand) + generations + transient stubs, discovery
  lists, entity load/decode + local metadata, the session reducer fork
  (local commits, inbox `connectionState` synthesis, `subscribe_to`
  desugar with `method=`, best-effort unsubscribe), persistence /
  rehydration. Builds the kitchen-sink **Python** tester backend (on
  the Part 2 library) + spawns it over the wire for integration tests.
- **Part 4 — PanelViewer refactor** (`part4_panelviewer.md`): extract
  a generic, connector-agnostic PanelViewer from `@fxtr-ui/workstation`;
  injected panel registrations; relocate the four existing panels
  behind workstation-specific registrations; per-connector roots/debug
  panels; route layout persistence through the shared storage handle.
- **Part 5 — demo** (`part5_demo.md`): lives under `examples/`; full
  PanelViewer shell over the HTTP connector + the **Python** stateless
  backend — entities, dynamic dispatch via a **WS** dispatch service
  with real hot reload (Part 1 + the WS dispatcher), GET + POST
  services returning data / entities / undiscoverable services, and
  loading those.
- **Part 6 — HTTP provenance** (`part6_http_provenance.md`, *scoped, not built*):
  let the HTTP surface carry both data- and prov-level ids (the pin view +
  service-returned entity-ref escape), so a provenanced entity's provenance can
  reach the client. The blocker today: `/pins` returns `{name: id_string}` and an
  `id_string` can't express a layered id. Serve-side complement of 7b part 5.
- **Part 7 — frontend provenance** (`part7_frontend_provenance.md`, *stub,
  high-level*): the HTTP connector + frontend consume layered ids so provenance
  flows through the HTTP-managed system the same way the workstation path does
  (parity target). A complex step, to be planned in detail against the frontend
  code; feeds the 7c demo's end-to-end provenance-on-the-client goal.

## Deferred follow-on — relocate `frontend_lib` to a top-level project

Once 7a lands, the frontend UI is no longer specific to the workstation
server (it carries two connectors, a generic PanelViewer, and a portable
`Connector` abstraction). At that point `frontend_lib/` should move out
of `projects/fxtr_workstation/` to its own top-level `projects/` entry.
**Deliberately deferred to after 7a, as a dedicated atomic commit:** the
`@fxtr-ui/*` package *names* don't change (only the physical path), so
nothing in 7a depends on the final location; doing it mid-feature would
only stale the file:line re-orientation pointers throughout these design
docs while they're still being executed against. The move is a path-only
refactor touching the pnpm-workspace globs, tsconfig/vitest configs,
relative imports + integration-harness paths, the workstation server's
static mount / `debug_ui_dist_path`, the nix renderer build
(`lib.fxtr.esbuildRenderer`), and the CLAUDE.md/CODE_OVERVIEW docs — best
reviewed and bisected on its own, once the new `connector-http` /
`panel-viewer` packages exist so they all move together.

## Deferred follow-on (after Part 5) — wire the stragglers into the typecheck graph — **DONE**

**Resolved.** Both `@fxtr-ui/agent-plugin-renderer` and
`examples/fxtr_workstation_demo/frontend` are now in the root
`tsconfig.json` `references` (so `pnpm check` covers them). The surfaced
errors were fixed at the source: the type-predicate took `unknown`
instead of `ResultValue`; `JSX.Element` → `ReactElement` (robust under any
consumer's `types`, so no examples-tsconfig change was needed); the
`exactOptionalPropertyTypes` prop got a conditional spread; the two
`string | undefined` regex-group reads got guards. The package's **tests**
had also bitrotted (the mock `CallbackInboxStub` predated the part6
`connectionState` / `subscriptionBreakReason` members; plus
invariant-generic + `noUncheckedIndexedAccess` issues) — fixed too. `tsc`
+ `biome` clean; 46 agent-plugin-renderer tests pass. The original
write-up is kept below for context.


Surfaced while landing Part 4 (see part4's "Implementation notes"): two
packages are **not** in the root `tsconfig.json` `references`, so
`tsc --build` never typechecks them and they've accumulated latent type
errors —

- `examples/fxtr_workstation_demo/frontend` (the assembly-stage demo), and
- `@fxtr-ui/agent-plugin-renderer` (which the examples demo depends on).

Adding the examples frontend to `references` drags in agent-plugin-renderer
and surfaces **5 pre-existing type errors** (3 trivial — an
`exactOptionalPropertyTypes` mismatch on `JsonRefTreeProps.initiallyExpanded`
+ two `string | undefined → string` guards; 1 medium — a
`StructuredUserMessage`/`ResultValue` type-predicate mismatch; 1
architectural — `Cannot find namespace 'JSX'`, caused by the examples
tsconfig's `types: ["vite/client"]` leaking into agent-plugin-renderer's
source typecheck, since cross-package types resolve from source, not from
`.d.ts` boundaries). None relate to the panel-viewer work, so they were
left alone.

**Deferred to after Part 5, as its own task:** add both packages to the
root `references`, fix the 5 errors, and decide whether agent-plugin-renderer
needs its own reference (so it's checked under a React-aware tsconfig) and/or
the examples app should relax its `types` restriction. Self-contained;
unrelated to 7a's connector/viewer work. Until then the examples demo +
agent-plugin-renderer typecheck only at Vite build time, not in `pnpm check`.
