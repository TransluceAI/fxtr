# old_typescript — Archived TypeScript packages

Deprecated TypeScript packages that have been **subsumed** by the
`@fxtr-ui/*` frontend substrate in
[`projects/fxtr_workstation/frontend_lib/`](../../projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md).
They are kept here for reference rather than deleted.

Two generations of frontend code live here:

1. The original **`@fxtr/react` substrate + report packages + example
   apps** (formerly under `v2/ts/packages/`). These were the first
   React bindings + reports rendering framework + demos built directly
   on `@fxtr/core`.
2. The original **`@fxtr-workstation/*` panel-viewer prototype**
   (formerly under `projects/fxtr_workstation/ts/`) — an interactive
   dockview-style panel viewer over fxtr2 entities, built on
   `@fxtr/core` + `@fxtr/react` + `@fxtr/react-dispatch`. It was never
   wired into the workstation server; the current shell is
   `@fxtr-ui/workstation` in `frontend_lib`.

## Workspace status

These packages remain part of the **repo-root pnpm workspace** (see
the `spikes/old_typescript/*` glob in
[`../../pnpm-workspace.yaml`](../../pnpm-workspace.yaml)) on purpose:
keeping them installed/typechecked/linted means they don't silently
bitrot. `pnpm install` / `pnpm check` / `pnpm test` / `pnpm lint` from
the repo root cover them. They depend (via `workspace:*`) on the
still-maintained `@fxtr/core`, `@fxtr/http-server`,
`@fxtr/http-client`, and `@fxtr/mdx-transform` packages, which stay in
`v2/ts/packages/` (their tsconfig `references` point back there with
`../../../v2/ts/packages/...`).

This whole tree — the glob, the root `tsconfig.json` references, and
the `vitest.config.ts` include — can be removed wholesale once we're
confident nothing here needs resurrecting.

## Packages (the `@fxtr/react` substrate generation)

- **`@fxtr/react`** (`fxtr-react/`) — React bindings for the fxtr
  entity system. Provides `FxtrProvider`, `useFxtrLoad`
  (Suspense-based entity loading), `useFxtrLoadQuery` (non-Suspense
  `{ data, loading, error }` alternative, ref form + callback form),
  `useFxtrScope` (escape hatch for programmatic scope access), and
  `FxtrSubscope` (cache isolation / scope bridging). Peer
  dependencies: `react` (>=18), `@fxtr/core`. Source: `context.tsx`
  (`FxtrProvider`, `FxtrContext`), `hooks.ts` (`useFxtrLoad`,
  `useFxtrLoadQuery`, `useFxtrScope`), `FxtrSubscope.tsx`.
- **`@fxtr/react-dispatch`** (`fxtr-react-dispatch/`) — Type-driven
  dispatch of fxtr entities to React renderers. See its
  `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/reports-entities`** (`fxtr-reports-entities/`) — Report
  entity definitions (`RootReport`, `SubReport`, `SimpleComment`,
  `LinkedSVGFigure`). Pure data, no React. See its
  `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/reports-react`** (`fxtr-reports-react/`) — Report rendering
  framework: dispatch keys, navigation, margin notes, `<ReportShell>`.
  See its `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/reports-react-handlers`** (`fxtr-reports-react-handlers/`) —
  Standard renderers for report entities. See its
  `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/examples`** (`examples/`) — Minimal example: entity
  definitions, server, CLI client, web client. See its
  `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/example-mdx-rendering`** (`example-mdx-rendering/`) — Demo:
  MDX-to-fxtr rendering with server + React client. See its
  `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/example-demo-report`** (`example-demo-report/`) — Demo: full
  reports pipeline with `ReportShell`. See its `LOCAL_CODE_OVERVIEW.md`.
- **`@fxtr/example-demo-fingerprinting-report`**
  (`example-demo-fingerprinting-report/`) — Demo: a fingerprinting
  report variant of the full reports pipeline. See its
  `LOCAL_CODE_OVERVIEW.md`.

## Packages (the `@fxtr-workstation/*` panel-viewer prototype)

- **`@fxtr-workstation/core`** (`fxtr-workstation-core/`) — the
  dockview-style panel-viewer shell (breadcrumb bar, navigation,
  history roots, entity panels).
- **`@fxtr-workstation/theme`** (`fxtr-workstation-theme/`) — design
  tokens / theming for the prototype shell.
- **`@fxtr-workstation/example-dev`** (`example-dev-workstation/`) —
  dev harness app (its own server) for the prototype.
- **`@fxtr-workstation/example-demo-fingerprinting`**
  (`example-demo-fingerprinting-workstation/`) — fingerprinting demo
  app for the prototype.

The prototype's original overview, plans, and reference material are
preserved alongside the packages:

- `workstation_ts_CODE_OVERVIEW.md` — the full architecture overview of
  the prototype shell.
- `workstation_ts_plans/` — the prototype's `plans/` tree.
- `workstation_ts_reference_info/` — design-system prototype HTML and
  other reference material.

(Relative links inside `workstation_ts_CODE_OVERVIEW.md` predate the
move; `packages/X`, `plans/`, and `reference_info/` there now
correspond to `X/`, `workstation_ts_plans/`, and
`workstation_ts_reference_info/` here.)
