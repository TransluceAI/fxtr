# fxtr stateless HTTP demo

A full **PanelViewer** shell running over the **stateless HTTP connector**
(`@fxtr-ui/connector-http`) against a **Python stateless backend** — the
end-to-end integration check for fxtr 7a (Parts 1–4). It exercises:

- **Entities** — pinned `Note` / `Photo` entities, browsable in the roots
  panel and openable as ref panels (RefLink, dispatch, metadata views).
- **Dynamic dispatch with hot reload** — a **WS** dispatch service +
  compiled renderer entities. The dynamic dispatcher resolves a renderer
  per entity type and renders it through the shell dispatch keys. Editing
  a renderer and **bouncing the backend** re-renders without a page
  reload (the ws drop flips the dispatcher's reload inbox to `broken`
  → caches wipe → re-resolve), as does the "reload everything" button in
  the **Dispatcher** debug panel.
- **Services** — a GET `echo`, a POST `data` (returns an entity, an
  *undiscoverable* service descriptor, and an error), the WS `dispatcher`,
  and a WS `stopwatch`. All carry `$fxtr.type` + a `service_type_id` in
  discovery. Two of them have **interactive renderers** (dispatched on
  the service type, same as entities):
  - **echo** — a FullPanel with a text input + button that invokes
    `echo(value)` from inside the renderer and shows the result.
  - **stopwatch** — a FullPanel that subscribes to the service; the
    backend pushes an incrementing count once a second over the ws
    connection and the renderer shows the latest tick.
- **Stateless persistence** — connector sessions + shortId table and the
  viewer's dockview layout share **one `sessionStorage` handle**; the
  **Storage** debug panel dumps it. Reload the page and ref-panel sessions
  rehydrate from storage.

## Layout

```
examples/fxtr_http_demo/
├── pyproject.toml          # uv project (fxtr2 + fxtr_workstation, editable)
├── server/
│   ├── demo_backend.py     # the stateless backend (Part 2 service layer + WS dispatch service)
│   └── renderers/{note,photo}/index.tsx   # renderer templates, esbuild-compiled at startup
└── web/                    # Vite app (pnpm workspace member)
    └── src/
        ├── server.ts       # orchestrator: spawns the backend, then Vite
        └── client/         # main.tsx (externals install) + App.tsx + panels.tsx
```

## Running

From the repo root (one `pnpm install` links the workspace; Node + `uv`
must be on `$PATH`):

```sh
pnpm --filter @fxtr-http-demo/web dev
```

The orchestrator (`web/src/server.ts`) starts the Python backend via
`uv run` (auto-syncing the example's env on first run + esbuild-compiling
the renderers), reads its `READY <url>` line, injects it as
`VITE_HTTP_BACKEND_URL`, and starts Vite. Open the printed URL
(http://localhost:3465). Ctrl-C tears both halves down.

## Hot reload

Baseline (no file-watcher): edit a renderer in `server/renderers/<name>/`
and **restart the backend** (Ctrl-C the dev process and re-run, or just
restart it) — the frontend's reload inbox goes `broken` on the ws drop
and the dispatcher wipes its caches and re-resolves. While connected, the
**Dispatcher** debug panel's "reload everything" button forces the same
wipe.

## WS vs GET dispatch

The demo publishes the dispatch service as a **WS** service, which buys
hot reload (reload-everything notifies) at the cost of routing `resolve`
over the ws connection. Flipping it to a **GET** service is a
*server-only* change (`add_rest_service` instead of `add_ws_service`):
`resolve` becomes CDN-cacheable and renderer delivery still rides
`/data`'s immutable caching, but `subscribe` goes inert (no hot reload).
See `plans/phase0/part7a_stateless_ws_server/design/07_dynamic_dispatch.md`.
