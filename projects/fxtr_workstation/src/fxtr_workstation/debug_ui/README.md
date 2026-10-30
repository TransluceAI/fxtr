# fxtr-workstation debug UI

Tiny standalone Vite + React + TS app that subscribes to the
workstation's `/client` endpoint and renders three live-updating
tables (RPC connections, tracked services, tracked entities).

This is a **debug viewer**, not the full panel-viewer client. It is
not intended to grow much beyond what's already here.

## Build

The project uses [pnpm](https://pnpm.io/) (declared via
`packageManager` in `package.json`):

```sh
cd fxtr_workstation/debug_ui
pnpm install
pnpm build       # tsc + vite build, writes dist/
pnpm typecheck   # tsc only
```

`pnpm build` produces `dist/`. The Python package mounts that
directory at `/debug` via Starlette `StaticFiles` when
`WorkstationServer` is constructed with
`debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path()`
(or any explicit override). With the workstation running on
`ws://127.0.0.1:<port>`, open `http://127.0.0.1:<port>/debug/`.

## Develop

```sh
pnpm dev
```

Vite serves on its own port (5173 by default); the debug UI's
`ws_client.ts` derives the workstation URL from `window.location`,
so dev-mode connections point at the Vite host. For a useful
dev loop, run `pnpm preview` against `dist/` while a workstation
server is up — same-origin requests work without proxying.

(A vite proxy config could fix the dev story; out of scope for
stage 2.)

## Why pnpm?

Faster installs and a smaller `node_modules` footprint via
hard-linked content-addressed storage. The `packageManager` field
in `package.json` declares pnpm as the canonical tool; corepack-
aware setups will use the right version automatically.
