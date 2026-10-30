# Part 5 — Demo

A full PanelViewer shell running over the HTTP connector against the
**Python** stateless backend, showing the whole stack end-to-end —
including dynamic dispatch with **real hot reload** (Part 1 + a WS
dispatch service). Lives under `examples/`. Depends on Parts 1–4.

Design rationale: [`overview.md`](overview.md) §Parts (Part 5) +
[`design/07_dynamic_dispatch.md`](design/07_dynamic_dispatch.md).

## Resolved decisions (2026-06-15)

Scoped against the landed Parts 1–4 (connector allocate surface,
`loadEntityData`, `classifyShortId`, lists, persistence all present;
PanelViewer takes `connector` + `dispatcher` + `panelRegistrations` +
`storage` + `defaultPanelId`):

- **Location:** `examples/fxtr_http_demo/` with `server/` (Python) +
  `web/` (Vite), mirroring `examples/fxtr_workstation_demo/`.
- **Dispatch service is hand-rolled.** The workstation's
  `BasicDynamicDispatchImpl` is bound to the workstation RPC /
  service-registration and can't be reused; the demo backend implements
  a fresh **Part-2 WS service** exposing `resolve` / `subscribe`. It
  reuses `compile_renderer` + the dispatch entity/notification types
  (`RendererEntity` / `RendererResolution` / `ReloadEverythingNotification`)
  from `fxtr_workstation.dynamic_dispatch`, so **the demo backend depends
  on `fxtr_workstation`** — accepted for an example (the workstation demo
  backend already does). Relocating those helpers to a neutral home is a
  possible later cleanup, not done here.
- **Renderers:** reuse/adapt the `note` / `photo` templates from
  `fxtr-ui-dynamic-dispatch-demo/src/renderers/`.
- **Hot reload = baseline only:** backend bounce (WS drop → `broken` →
  Part 1 wipe + re-resolve) + the manual "reload everything" button in
  the dispatcher debug view. **No file-watch** live reload at this stage.
- **Demo panels:** an HTTP **roots panel** (over `HttpConnector`'s lists,
  records carrying `service_type_id`), a **storage-inspector** debug panel
  (stateless-only affordance), and a small demo-owned **dispatcher-debug**
  panel rendering `dispatcher.debugView()` (the demo must **not** depend
  on `@fxtr-ui/workstation`, so it re-implements the ~5-line wrapper).
- **Typecheck graph:** add `examples/fxtr_http_demo/web` to the root
  `tsconfig.json` `references` from the start (it uses its own renderers,
  so it doesn't drag in the unchecked `@fxtr-ui/agent-plugin-renderer`).
- **Part 3 loose ends:** close the 4 unchecked Part 3 boxes here *if
  easy* — especially the design/03 **parity test** (now runnable since
  both a workstation and the Python HTTP backend exist).

## Re-orientation pointers

| What | Where |
|---|---|
| Backend tester-script shape | Part 2's tester script + `fxtr-ui-connector-workstation/tests/_demo_workstation.py` |
| Existing frontend demo app shape (Vite over a connector) | `fxtr-ui-workstation-demo/` |
| Renderer compile + external set | `fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/compile.py` |
| Import-map shim | `fxtr-ui-react-dispatch-dynamic-shim/src/install_externals.ts:146-191` |
| Dispatcher resolve / reload-subscribe | `fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts` |

## Scope (the demo checklist)

A single example demonstrating, over the stateless connector:

1. **Entities** — pinned entities browsable + loadable (roots panel,
   ref links, JsonRefTree, metadata views).
2. **Dynamic dispatch with hot reload** — a **WS** dispatch service +
   renderer entities + type entities; editing a renderer and bouncing
   the backend (or hitting the manual reload) re-renders without a page
   reload (Part 1 reload-on-break + on-subscribe reload).
3. **Services** — a GET service and a POST service that return data /
   entities / an **undiscoverable** service descriptor, and the UI
   loading those results.

## Steps

### A. Python demo backend (`examples/fxtr_http_demo/server`)
- [x] Build on Part 2's registry + endpoints (a trimmed demo-focused
  variant of the kitchen sink). Pins demo Note/Photo entities, compiled
  **renderer entities** (`{module: bytes}`, `compile_renderer` with the
  shim's external set), and builtin **type entities** for every
  dispatchable entity/service; services implement `$fxtr.type`.
- [x] **WS dispatch service** — `resolve(typeRef, keyUuid)` over a
  type-id→renderer table (returns `{subtype_key, renderer}`) +
  `subscribe(inbox)` emitting a reload-everything notify immediately.
  (Hand-rolled over the Part 2 ws layer; `BasicDynamicDispatchImpl` is
  RPC-bound and not reusable. Baseline hot reload = backend bounce /
  manual button; no file-watcher.)
- [x] GET `echo` service; POST `data` service returning an entity + an
  undiscoverable http descriptor (+ a `boom` error). Discovery carries
  `service_type_id` on every service. Plus a WS `stopwatch` service
  (post-scope addition) with a per-second ticker.
- [x] A run entrypoint (`uv run python server/demo_backend.py`) printing
  `READY <url>`.

### B. Frontend demo app (`examples/fxtr_http_demo/web`, Vite)
- [x] `HttpConnector({ baseUrl, storage: sessionStorage })`; dynamic
  dispatcher over the connector; `installFxtrExternals` in `main.tsx`
  before first renderer import.
- [x] Renders `PanelViewer` with the connector, dispatcher, the **same
  `sessionStorage`**, `defaultPanelId`, and demo `panelRegistrations`:
  an HTTP roots panel (over the connector's discovery lists), a
  storage-inspector debug panel, and a demo-owned dispatcher-debug panel
  (`dispatcher.debugView()`, with the Part 1 reload button). Service
  renderers (echo invoke, stopwatch subscribe) dispatch on the service
  type.
- [x] Confirmed the dispatch path needs **zero** dispatcher changes
  beyond Part 1 (resolve invoke, `$fxtr.type`, renderer `loadEntityData`
  all map onto the connector) — the demo uses `createDynamicDispatcher`
  unchanged.

### C. Wire-up + verification
- [x] End-to-end (verified in-browser): entities load + render via the
  WS dispatch; GET/POST services return data/entities/undiscoverable
  descriptors; the echo + stopwatch service renderers invoke/subscribe;
  reload rehydrates ref-panel sessions from storage. Hot reload on
  backend bounce works (ws drop → reload inbox `broken` → re-resolve).
- [x] README with run instructions + the WS→GET dispatch tradeoff note.

CODE_OVERVIEW updated (the `@fxtr-ui/connector-http` section points to
the demo) + a local README in `examples/fxtr_http_demo/`.

## Notes / watch-fors
- Demo content specifics (exact entities/services/renderers) are scoped
  here, not earlier (overview Resolved-#16).
- The demo is the cross-cutting integration check for Parts 1–4; if
  something only shows up when the whole stack runs (e.g. a
  rehydration or hot-reload edge), capture it back in the relevant
  part doc.
- **Watch-for (renderer authors): the allocate-and-store contract bites
  subscriptions.** A renderer that subscribes must store *both* the
  inbox **and** the `subscribe_to` invoke slot in `ownStorage`. Storing
  only the inbox (as the first stopwatch renderer did) leaves the
  subscribe-invoke slot unreferenced, so the session reducer's flush
  walk drops it — the invoke is never sent, no ws connection opens, and
  the subscription silently never starts. (An invoke-on-click renderer
  like `echo` only has the one result slot, so it's harder to get wrong.)
- **Closed in Part 5:** the Part 3 shared-`Storage` box (the demo passes
  one `sessionStorage` to both connector + viewer). **Still open:** the
  design/03 cross-backend **parity test** — it needs one entity served
  by both a workstation and the Python backend; the demo exercises the
  HTTP decode path but doesn't compare it against the workstation's.

Update CODE_OVERVIEW.md (final).
