# Step 1 — Workspace scaffolding

Lay down the TypeScript workspace for the workstation project under
`projects/fxtr_workstation/ts/`. Produces: a working `pnpm install`, a
working `tsc --build` with project references, a working `biome check`.
No application code yet — only empty `src/index.ts` stubs in each
package so builds succeed.

## Decisions

- **Workspace membership: Option B** — workstation packages join v2/ts's
  pnpm workspace via a relative glob in `pnpm-workspace.yaml`.
  Packages physically live under `projects/fxtr_workstation/ts/packages/`
  but are workspace members of v2/ts. Single lockfile, single install.
  A note goes in `v2/ts/CODE_OVERVIEW.md` (plus `projects/fxtr_workstation/CLAUDE.md`)
  explaining (1) why we piggy-back on v2's workspace, (2) that the
  fxtr_workstation project is experimental, and (3) that the
  `projects/fxtr_workstation/` directory is expected to grow non-TS code
  too, which is why we keep it separate from v2 even though its TS
  packages ride along on v2's workspace.
- **Package scope: `@fxtr-workstation/*`** — visually separates the
  workstation app layer from core fxtr, and leaves room for the
  package set to drift stylistically from `@fxtr/*` without conflict.

## Workspace layout to create

Because workstation packages join v2/ts's workspace, there's no root
`package.json`, `pnpm-workspace.yaml`, or `biome.json` at
`projects/fxtr_workstation/ts/` — those live at `v2/ts/`. The only
shared config here is a small `tsconfig.base.json` that extends v2's,
so individual package `tsconfig.json` files only need to walk up two
directories instead of five.

```
projects/fxtr_workstation/ts/
├── tsconfig.base.json                # extends tsconfig.base.json
└── packages/
    ├── fxtr-workstation-theme/
    │   ├── package.json              # name: @fxtr-workstation/theme
    │   ├── tsconfig.json             # extends ../../tsconfig.base.json
    │   └── src/
    │       └── index.ts              # export {} placeholder
    ├── fxtr-workstation-core/
    │   ├── package.json              # name: @fxtr-workstation/core
    │   │                              # deps: @fxtr/core, @fxtr/react, @fxtr/react-dispatch,
    │   │                              #       @fxtr-workstation/theme, dockview, @base-ui/react;
    │   │                              # peer: react >=18
    │   ├── tsconfig.json             # composite, jsx: react-jsx
    │   └── src/
    │       └── index.ts
    └── example-demo-fingerprinting-workstation/
        ├── package.json              # name: @fxtr-workstation/example-demo-fingerprinting
        │                              # deps: @fxtr-workstation/core, @fxtr/http-client,
        │                              #       react, react-dom
        │                              # devDeps: vite, @vitejs/plugin-react, @types/react, etc.
        ├── tsconfig.json
        ├── vite.config.ts
        ├── index.html
        └── src/
            └── main.tsx              # ReactDOM.createRoot stub
```

Changes to `v2/ts/` (Option B mechanics):

- `pnpm-workspace.yaml` — add `"../../projects/fxtr_workstation/ts/packages/*"` to the `packages` list.
- `tsconfig.json` — append three `{ "path": "..." }` references pointing into workstation packages.
- `CODE_OVERVIEW.md` — update Workspace Setup section to mention the
  cross-directory inclusion and the rationale.
- `package.json` — extend the `lint` / `lint:fix` / `format` scripts to
  cover workstation packages alongside v2's `packages/`.

Additions to `projects/fxtr_workstation/CLAUDE.md`:

- A short note pointing at `pnpm-workspace.yaml` as the workspace
  root for the `ts/` subtree here.

Tooling conventions (inherited from v2/ts):

- Package `exports` point to `./src/index.ts` for hot dev; flip to
  `./dist/...` at publish time.
- React `^19`.
- Vite `^6` + `@vitejs/plugin-react` for the demo (copied from
  `v2/ts/packages/example-demo-fingerprinting-report/vite.config.ts`).
- `jsx: "react-jsx"` per-package in tsconfigs that contain TSX.
- No vitest in phase 0.
- No project references across the v2 ↔ workstation boundary — packages
  find each other via `workspace:*` + pnpm's symlinked `node_modules` +
  `exports` pointing at source. If tsc incremental builds start
  suffering, add project references later.

## Implementation todos

- [x] Decide workspace option A vs B. → B.
- [x] Decide package scope. → `@fxtr-workstation/*`.
- [x] Edit `pnpm-workspace.yaml` to include workstation packages.
- [x] Edit `tsconfig.json` to reference workstation packages.
- [x] Edit `package.json` lint/format scripts to include
      workstation paths.
- [x] Update `v2/ts/CODE_OVERVIEW.md` Workspace Setup section.
- [x] Add a short note to `projects/fxtr_workstation/CLAUDE.md`.
- [x] Create `projects/fxtr_workstation/ts/tsconfig.base.json` (extends
      v2's).
- [x] Create `packages/fxtr-workstation-theme/` (package.json,
      tsconfig.json, empty `src/index.ts`).
- [x] Create `packages/fxtr-workstation-core/` (package.json,
      tsconfig.json, empty `src/index.ts`).
- [x] Create `packages/example-demo-fingerprinting-workstation/`
      (package.json, tsconfig.json, vite.config.ts, index.html,
      `src/main.tsx` stub).
- [x] Run `pnpm install` at `v2/ts/`; verify `workspace:*` links
      resolve. → 17 projects, 3 new, deps linked.
- [x] Run `pnpm check` (tsc --build) from `v2/ts/` and verify all
      packages compile. → clean.
- [x] Run `pnpm lint` from `v2/ts/` and verify clean exit. → clean
      after one biome formatting autofix.
- [x] Update `projects/fxtr_workstation/ts/CODE_OVERVIEW.md` with the
      concrete workspace layout.

## Notes from implementation

- The top-level `projects/fxtr_workstation/ts/tsconfig.base.json` extends
  `../../../tsconfig.base.json` — three levels up, not two. (Got
  this wrong on the first pass.)
- biome reformatted the demo `tsconfig.json`'s `references` array onto
  a single line. Kept the autofix.
- Didn't need to set `composite: true` in workstation package
  tsconfigs — it's inherited from v2's base via our local base.
- Didn't add project references across the v2 ↔ workstation boundary
  and builds/lints work. Leaving it that way until proven otherwise.

## Out of scope for step 1

- No implementations of dispatch keys, shell components, renderers,
  interactions, or demo content — that's step 2 onward.
- No CSS tokens yet — step 2 is fxtr-workstation-theme.
- No dockview integration — step 3.
- No pre-commit hook wiring. The root repo's pre-commit hooks cover
  formatting/linting if/when workstation packages are added to their
  include globs; we can verify that after step 1 lands.
