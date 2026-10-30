# Stage 4, Chunk 6 — Demo skeleton + mutate

First end-to-end runnable demo of the `@fxtr-ui/*` stack against a real workstation. Confirms the providers, hooks, and per-session-store wiring work in the browser. Scope is deliberately narrow: connection status, session list, "create session", panel grid with one mutate-bound text field per panel. No load, no invoke — those land in chunks 7 and 8.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- New package `@fxtr-ui/demo` at `frontend_lib/packages/fxtr-ui-demo/`.
- Vite-based React app.
- `src/demo_workstation.py` — long-running workstation backend (adapted from the integration tests' Python harness, but parked indefinitely rather than exiting on SIGTERM).
- `src/server.ts` — orchestrates: spawns the Python workstation, reads `READY <url>` from its stdout, starts Vite's dev server with that URL injected as an env var.
- React app modules:
  - `client/main.tsx` — entry, mounts `<App />`.
  - `client/App.tsx` — top-level: instantiates `WorkstationConnector`, wraps a connector-scope `<JotaiProvider>`, lays out status bar / sidebar / panel grid.
  - `client/ConnectionStatusBar.tsx` — `useAtomValue(connector.connectionStatus)`, renders the four states.
  - `client/SessionSidebar.tsx` — `useAtomValue(listSessions.records)` for the live list + a "New session" button calling `connector.createSession({})`.
  - `client/PanelGrid.tsx` — local component-state list of `{ id, session }` panels; the sidebar's "open this session in a panel" action pushes onto it; panel close button removes.
  - `client/Panel.tsx` — wraps `<FxtrSessionProvider session={...}>`; reads `session.lifecycle` for the status badge; uses `useSubAtom(session.userState, "message")` (after a null-narrow on `useSessionState()`) for the text input.

Out (deferred):

- Loading entities. → Chunk 7 (adds a "load entity" button + display).
- Invoking services. → Chunk 8.
- Polished styles, theming.
- React-layer component tests. → Test chunk.
- Two-views-of-same-session-id affordance (was OQ-α in the stage doc) — start with one panel per session.

## Design decisions

### Orchestration: a single `tsx src/server.ts` entrypoint

The existing `example-dev-workstation` runs both fxtr HTTP routes and the Vite dev middleware from one `server.ts` (using `node:http` + Vite's middleware mode). For our demo, the backend is a separate Python process (the workstation), so the orchestration is slightly different:

1. `server.ts` spawns the Python script via `uv run python src/demo_workstation.py`.
2. Reads `READY <ws-url>` from its stdout (same convention as the integration harness).
3. Starts Vite's dev server (port 3461 to avoid collision with example-dev-workstation's 3460) with the workstation URL injected as a `define` constant (or `import.meta.env.VITE_WORKSTATION_URL`).
4. On SIGINT/SIGTERM, terminates the Python child and shuts down Vite.

The Python script lives at `src/demo_workstation.py` and largely mirrors the integration test's `_demo_workstation.py` — same `WorkstationServer` setup, same `e-test` / `s-test` entity + service. The only divergence: the demo version doesn't print `READY` and exit; it prints `READY <url>` and parks until killed. (We can extract a shared helper later if a third consumer wants the same harness.)

### Frontend layout

```
┌──────────────────────────────────────────────────────────────┐
│ ws://… [connection-status badge]                            │
├─────────────────┬────────────────────────────────────────────┤
│ Sessions        │ Panel grid                                 │
│ ─────────       │                                            │
│ [+ new]         │  ┌──────────┐  ┌──────────┐                │
│                 │  │ Panel A  │  │ Panel B  │                │
│ • sess-1 [open] │  │ lifecycle│  │ …        │                │
│ • sess-2 [open] │  │ msg: …   │  │          │                │
│ • sess-3 [open] │  │ [x]      │  │          │                │
└─────────────────┴────────────────────────────────────────────┘
```

`PanelGrid` keeps a `useState<{ id: number; session: WorkstationSession }[]>`. Sidebar's "open" action calls `connector.attachSession(sessionRecord.session_id)` and pushes the result onto the grid's list.

### Session-state shape

For chunk 6, the user state is a single `{ message: string }` dict. Panel renders an `<input>` bound via `useSubAtom(session.userState, "message")` (with a fallback for the initial `userState === null` state — either render disabled until the snapshot arrives, or write a default on mount). The simplest is: if `useSessionState()` is null, render the input with placeholder text; on first edit, set the whole shape.

### Connector store

One store per connector. `<JotaiProvider store={connector.store}>` wraps the whole app (so the connector's reactive lists + connection status work). Inside, each `<FxtrSessionProvider session={...}>` re-wraps with the session's store — which by chunk-5's design defaults to `connector.store`. So in practice it's all one store, with session atoms naturally isolated by atom-reference identity. We can layer per-session stores later if it matters.

### Workstation URL discovery

Vite picks up `VITE_WORKSTATION_URL` from the environment that `server.ts` sets. The client reads `import.meta.env.VITE_WORKSTATION_URL` and passes it to `new WorkstationConnector({ url: ... })`.

## Implementation steps

1. [x] Create `frontend_lib/packages/fxtr-ui-demo/` with `package.json` (vite, react, all `@fxtr-ui/*` deps via workspace), `tsconfig.json`, `vite.config.ts`, `index.html`.
2. [x] Register the package in `tsconfig.json`'s `references` list. Run `pnpm install`.
3. [x] Add `src/demo_workstation.py` — adapted from the integration test's harness; uses different names (`demo-note` / `demo-service`) so the demo entity/service don't collide with the test fixture if both are running.
4. [x] Add `src/server.ts` — spawn Python, await READY, set `VITE_WORKSTATION_URL`, start Vite dev server, wire SIGINT teardown.
5. [x] Implement React entry + UI components: `App` lifts the connector + JotaiProvider; `ConnectionStatusBar` shows wire status; `SessionSidebar` lists sessions + "+ new"; `Panel` wraps `<FxtrSessionProvider>` and writes `{ message }` via `useSetAtom(session.userState)`. (Did NOT factor out a separate `PanelGrid` — the panel-grid layout is a simple grid in `App.tsx`; not enough surface to warrant its own file.)
6. [x] Verify by running `pnpm dev` in the package: workstation boots, vite serves on port 3461, `VITE_WORKSTATION_URL` is correctly injected into transformed module output (verified via curl).
7. [x] `pnpm check && pnpm lint` all green.
8. [x] Update `frontend_lib/CODE_OVERVIEW.md` with the new package.

## Notes from implementation

- Pyright flags the Python imports as unresolved because the workstation Python package isn't on its analysis path; same warnings already appear on the test harness's `_demo_workstation.py`. The imports resolve at runtime via `uv run`. Not a real issue.
- I considered using `@fxtr-ui/react`'s nascent `<ConnectorProvider>` for the connector-scope, but we dropped that earlier in chunk 5 — the demo uses `<JotaiProvider store={connector.store}>` directly and passes the typed `connector` reference as a prop to `ConnectionStatusBar` and `SessionSidebar`. Two prop-drillings; not enough to justify a context.
- The demo wraps each panel in `<FxtrSessionProvider>` but the per-session-store override isn't exercised yet — by default sessions share the connector's store (the chunk-5 design). When chunks 7 / 8 add load and invoke, the "two panels on the same session_id, isolated UI state" case will become more interesting; we can layer in `createStore()` per panel then.
- Did not write a `Vite-side env.d.ts` for `import.meta.env.VITE_WORKSTATION_URL`; `vite/client` types in tsconfig's `"types"` array provide the augmentation. Verified by `pnpm check`.

## Post-implementation fix: React StrictMode tearing down the connector

First browser-side smoke test surfaced "WebSocket is closed before the connection is established" and `ConnectorClosedError` on every subsequent `createSession`. Root cause: I initially put the connector in `useMemo` with an `useEffect` cleanup calling `connector.close()`. In React 18 StrictMode (dev only), effects run mount → cleanup → mount, which closes the connector before the WebSocket finishes its opening handshake. The connector's internal connection-status observer sees the connection go to `"closed"` and self-closes; subsequent calls throw `ConnectorClosedError`.

Fix: hoist the connector to a module-level singleton — created at module load, lives for the page's lifetime, no React effect lifecycle involved. The browser closes the underlying socket on page-unload, so we don't leak.

Also added lightweight `console.log` traces at the demo level (`createSession`, `attachSession`/openPanel, `detach`/closePanel) so future similar issues are easier to spot in the devtools console without poking around the connector internals.

The same pattern would bite consumer apps; the connector's lifecycle is genuinely app-singleton-shaped, not React-component-shaped. Worth flagging in the React-package docs at some point.

## Post-implementation addition: bundled debug UI

The workstation server already ships a bundled debug UI (`WorkstationServer(debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path())` mounts it at `/debug`). Wired it up:

- `demo_workstation.py` constructs the server with `debug_ui_dist_path=...` and prints a `DEBUG_UI <http-url>` line after `READY`.
- `server.ts` reads both READY and DEBUG_UI from stdout (refactored `readReadyLine` → `readReadyLines`), and sets `VITE_WORKSTATION_DEBUG_UI_URL` alongside `VITE_WORKSTATION_URL`.
- `ConnectionStatusBar` shows a "debug UI ↗" link next to the workstation URL when the env var is present. Opens in a new tab.

This gives the demo built-in server-side observability (session refcounts, preserved seqs, in-flight invokes, etc.) without us building a custom debug overlay. Will be especially useful for chunks 7 (load) and 8 (invoke) when slot mechanics get more interesting.

## What chunk 7 will do

Add a "Load entity" button to each panel that allocates a load via `ctx.allocateLoad(connector.entityRef(entityShortId))` (entity short_id resolved from `connector.listEntities()`) and displays the result via `useResult`. Two panels on the same session demonstrate the dedup behavior end-to-end (one wire load shared between them).
