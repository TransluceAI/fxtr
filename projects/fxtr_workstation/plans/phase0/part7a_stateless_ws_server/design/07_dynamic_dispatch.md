# 07 — Dynamic dispatch over the stateless connector

Status: resolved. Two conclusions:

1. **The resolve/load path needs zero dispatcher changes** — every
   dispatcher dependency (resolve invoke, `$fxtr.type`, renderer
   `loadEntityData`) maps onto the new connector unchanged.
2. **Hot reload is turned on** via a small, connector-agnostic
   dispatcher change carved out as **Part 1** (resolved Q-I): the
   dispatcher wipes its renderer caches + resubscribes when its reload
   inbox's `connectionState` flips to `broken`, and the dispatcher
   debug panel gets a manual "reload everything" action. With that in
   place, publishing the dispatch service as a `{kind:"ws"}` descriptor
   that emits reload-everything gives real hot reload — and a backend
   redeploy (which drops the ws connection) self-heals: break → cache
   wipe → re-resolve → fresh renderers. The Part-5 demo ships the WS
   dispatcher.

## Re-orientation pointers

All in `frontend_lib/packages/fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts`:

| Dispatcher behavior | Lines | Over the stateless connector |
|---|---|---|
| Reload inbox: `allocateCallback()` + `allocateInvoke(inbox.handle, "subscribe_to", [dispatchServiceRef])` | 517-538 (alloc at 528-531) | inbox is client-local; `subscribe_to` desugars to a `subscribe` invoke on the dispatch service (05). GET-backed dispatch service ignores the callback → no events, ever. |
| Never reads `subscribe_to`'s return value; `reload_subscribe_result` only anchors the slot | 534-535 | errored/odd fills are harmless — confirmed degradation hinge |
| Reload reaction: stale-flip + cache wipe | 502-582 (flip 545-551, wipe 559-581) | never fires (no events); staleness wrap stays dormant |
| Renderer resolution: `allocateInvoke(dispatchServiceRef, "resolve", [typeRef, keyUuid])` | 946-952 | ordinary invoke; dispatch service published as a **GET** service → CDN-cacheable resolution |
| Service-type discovery: `allocateInvoke(stub, "$fxtr.type", [])` | 1227-1258 (invoke 1239-1243) | backend services implement the `$fxtr.type` method (01) |
| Entity-type discovery: from the loaded entity's `$fxtr.type` field | (entity effect factory) | `cborToResultValue` surfaces `$fxtr.type` as a stub leaf identically (03) |
| Renderer fetch + compile: `loadEntityData(rendererRef)` → `{module: Uint8Array}` → blob-URL `import()` | 445-474 (load 448, compile 458; default compile 792-813) | `connector.loadEntityData` returns the decoded renderer entity; bytes stay `Uint8Array` (03) |
| Errored resolve / type invokes → entry `phase: "errored"`, no auto-retry | 893-897, 1262-1270 | identical; remote failures arrive as errored fills (05) |

Import-map shim: `installFxtrExternals` must still run before the
first renderer import
(`fxtr-ui-react-dispatch-dynamic-shim/src/install_externals.ts:146-191`)
— purely an app-wiring concern, unchanged.

## What the backend must provide

For dispatch to function end-to-end (demo checklist):

1. A **dispatch service** implementing
   `resolve(typeRef, keyUuid) -> RendererResolution | null` where
   `RendererResolution = {subtype_key, renderer: <entity ref>}` —
   same shape as `WorkstationDispatcherProto.resolve`
   (`projects/fxtr_workstation/src/fxtr_workstation/dynamic_dispatch/dynamic_dispatch_types.py`).
   It also receives `subscribe(handle)` calls (from the dispatcher's
   reload subscribe). Published as either **GET** (resolve is
   CDN-cacheable; subscribe is inert, no hot reload) or **WS** (hot
   reload via reload-everything notifies; resolve rides the ws
   connection, not the CDN). The Part-5 demo uses **WS**.
2. **Renderer entities** (`{module: bytes}`, the `RendererEntity`
   shape) reachable via `/data` — compiled with the same external
   set as the import-map shim (cf.
   `basic_dynamic_dispatch/compile.py`'s external list).
3. **Type entities** for every dispatchable entity/service, also
   `/data`-reachable, and `$fxtr.type` methods on services.

CDN note: with a **GET** dispatch service, `resolve` args are only an
entity id_string + a uuid and the result is deterministic per renderer
table, so the whole resolve path is statically cacheable and renderer
delivery rides `/data`'s immutable caching — the fully-CDN deployment
story. The WS dispatch service trades that resolve-caching for hot
reload; renderer delivery still rides `/data` caching either way.

## Part 1 — the dispatcher reload change (setup)

A self-contained, connector-agnostic change scoped to
`@fxtr-ui/react-dispatch`'s **`createDynamicDispatcher`** (not the
generic `Dispatcher` surface), landing first so the WS dispatch service
"just works" when the backend arrives. Two pieces:

1. **Reload-on-break.** Today the dispatcher only resubscribes when
   the *session* reattaches (`dynamic_dispatcher.ts:517-526`), not
   when a subscription silently dies. Make its reload effect also watch
   the reload inbox's `connectionState` (the abstract
   `CallbackInboxStub.connectionState` from part6 stage4post1) and, on a
   transition to `broken`, **wipe the renderer caches + resubscribe on
   the existing inbox handle** (reusing the reception-path wipe,
   `:545-581`). Uniform across connectors: the workstation drives
   `connectionState` via `CallbackSubscriptionEvent`s; the HTTP
   connector synthesizes it on ws drop (design/05 §Inboxes). Note a ws
   drop leaves `inbox.state === "open"` but `connectionState ===
   "broken"`, which the existing `state === "closed"` branch misses —
   this is the gap.
2. **Manual reload.** Add a "reload everything" button **inside the
   dynamic dispatcher's own debug view** (`dynamic_debug_view.tsx`,
   built by `debugView()` `:719-741`) wired to an internal closure that
   runs the same wipe (+ resubscribe if broken). It surfaces wherever
   `debugView()` is rendered (e.g. the workstation's `DispatcherDebugPanel`)
   with **no change to that panel**, and **no public method on the
   `Dispatcher` interface**.

Both reuse the dispatcher's existing cache-wipe (factored into one
internal closure); the new triggers are `connectionState → broken` and
the button, alongside the existing received-`ReloadEverythingNotification`
trigger.

Guard against a resubscribe storm: react **once per `broken`
transition** (debounce on the state edge, not the level), so a source
that can't be reached doesn't spin reconnect attempts. A failed
resubscribe leaves the inbox `broken`/`idle` and waits for the next
genuine trigger (button, session re-attach, or a later successful
connect).

## Hot reload, on this connector

With Part 1 landed, republishing the dispatch service as a
`{kind: "ws"}` descriptor turns reload on with no further client
change: `subscribe(handle)` routes `ReloadEverythingNotification`-
equivalent notifies to the inbox and the existing reload effect wakes
up. The demo's WS dispatch service emits a reload-everything notify
**immediately on each `subscribe`** — this is what makes
cold-reload-after-redeploy self-heal: on a fresh page load the session
re-attaches, the dispatcher resubscribes (existing behavior), and the
server's on-subscribe reload wipes any stale persisted caches. A
**GET** dispatch service still works (resolution is CDN-cacheable) and
simply never emits reloads — the degraded mode.

## Cache staleness across backend redeploys — resolved

The dispatcher persists its caches in session userState
(`type_renderer_cache` etc.), which now survive reloads (parity with
workstation sessions). Stale caches could otherwise outlive a backend
redeploy — content addressing keeps the entries *valid* (renderer
entities are content-addressed; a changed renderer is a different id),
but they'd point at old renderers until cleared. **Resolved by Part 1
+ WS dispatch** (Q-I): a redeploy drops the ws connection → the reload
inbox goes `broken` → caches wipe + re-resolve → fresh renderers. The
manual reload action covers the "redeploy while still connected" /
dev-iteration case.
