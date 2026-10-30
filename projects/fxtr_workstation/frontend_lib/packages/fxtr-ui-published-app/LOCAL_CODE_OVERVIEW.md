# `@fxtr-ui/published-app`

The **demo-agnostic** viewer for any flake published by `fxtr_nix serve`: a Vite
SPA that mounts a `PanelViewer` over the stateless HTTP connector
(`@fxtr-ui/connector-http`). Nothing here knows about a particular flake's entity
types — it browses whatever the served `/discovery` lists and dispatches each
entity through the flake-built renderers.

It is the UI half of the `fxtr_nix serve` story: build it once (`pnpm --filter
@fxtr-ui/published-app build`), then `fxtr_nix serve <flake> --web-dist
<this>/dist` serves the built `dist/` at `/` on the same origin as the API.

## Mental model

- **Base URL = same origin by default.** `App.tsx` constructs `new
  HttpConnector({ baseUrl })` with `baseUrl = window.location.origin` (the
  served-static case), falling back to `VITE_PUBLISHED_BACKEND_URL` for the
  `vite dev` HMR case (pointed at a separately-running serve). Discovery URLs are
  relative and renderer blobs come from `/data`, so same-origin needs no plumbing.
- **Dispatcher by name.** `App.tsx` waits for a `dispatcher` ws service in the
  discovery list and builds a `createDynamicDispatcher` over it (resolve +
  reload-on-break). The renderer modules are built by nix per the flake's
  `uiPresets` and fetched at runtime as `/data` blobs.
- **Union externals.** `main.tsx` `installFxtrExternals(...)` installs the
  *union* of every renderer-facing package any served flake might import (base
  `@fxtr-ui/*` + `workstation` + `reports` + `agent-plugin-renderer` +
  `rd-types-channel` + `rd-types-stream`). This is the frontend analogue of
  `lib.fxtr.workstationExternals` / `DEFAULT_EXTERNALS`; **add a new
  renderer-facing package here once** (and as a dependency) so any flake's
  renderers resolve their bare imports against the host's bundled instances.
- **Generic panels.** `panels.tsx` registers Roots (discovery lists →
  `RefLink`s), a Storage inspector, and a Dispatcher debug panel — none of which
  reference any specific entity type.

## Files

```
fxtr-ui-published-app/
├── LOCAL_CODE_OVERVIEW.md
├── package.json        # @fxtr-ui/published-app (app: build = vite build); deps = the union externals + connector/viewer
├── tsconfig.json
├── vite.config.ts      # dev server on :3466 (HMR against a running serve)
├── index.html          # mounts /src/main.tsx
└── src/
    ├── main.tsx        # installFxtrExternals (union) + render <App/>
    ├── App.tsx         # HttpConnector (origin/env base url) + dynamic dispatcher + PanelViewer
    └── panels.tsx      # generic Roots / Storage / Dispatcher-debug registrations
```

## Running

- **Served (static):** `pnpm --filter @fxtr-ui/published-app build` →
  `uv run fxtr_nix serve <flake> --port 8080 --web-dist <this>/dist` → open the
  printed origin.
- **Dev (HMR):** run `fxtr_nix serve <flake> --port 8080` in one terminal, then
  `VITE_PUBLISHED_BACKEND_URL=http://127.0.0.1:8080 pnpm --filter
  @fxtr-ui/published-app dev` in another.
