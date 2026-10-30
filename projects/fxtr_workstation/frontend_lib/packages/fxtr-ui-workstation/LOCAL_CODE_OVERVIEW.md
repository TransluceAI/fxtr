# `@fxtr-ui/workstation`

The **workstation-connector panels** for the generic panel viewer.
After the panel-viewer split, this package is reduced to the four
built-in panels (roots + three debug surfaces) expressed as injected
`PanelRegistration`s, plus their debug tables and a connector-narrowing
hook. The generic dockview mechanics it used to contain now live in
`@fxtr-ui/panel-viewer`; this package **depends on that** and
**re-exports its full surface** (`export *`) so existing consumers that
import the shell / navigation / dispatch helpers from
`@fxtr-ui/workstation` keep resolving unchanged.

The dev surface is exercised end-to-end by sibling package
`@fxtr-ui/workstation-demo` (Python backend + Vite client), which
mounts `<PanelViewer>` (re-exported from `@fxtr-ui/panel-viewer`)
with `panelRegistrations={workstationPanelRegistrations}`.

## What lives here

```
fxtr-ui-workstation/
├── LOCAL_CODE_OVERVIEW.md
├── package.json
├── tsconfig.json
└── src/
    ├── index.ts                  # `export *` from @fxtr-ui/panel-viewer + the panels, params, registrations, useWorkstationConnector
    ├── useWorkstationConnector.ts # narrows the viewer's abstract Connector → WorkstationConnector
    └── panels/
        ├── registrations.ts               # workstationPanelRegistrations — the four panels as PanelRegistrations
        ├── RootsPanel.tsx                  # registry browser (stateless)
        ├── RootsPanelParams.ts             # {kind:"roots"} + ROOTS_PANEL_COMPONENT
        ├── WorkstationDebugPanel.tsx       # four resource tables (stateless)
        ├── WorkstationDebugPanelParams.ts  # {kind:"workstation_debug"} + COMPONENT
        ├── SessionDebugPanel.tsx           # per-session debug body
        ├── SessionDebugPanelParams.ts      # {kind:"session_debug", sessionId} + COMPONENT
        ├── DispatcherDebugPanel.tsx        # renders dispatcher.debugView()
        ├── DispatcherDebugPanelParams.ts   # {kind:"dispatcher_debug"} + COMPONENT
        └── tables/                # shared debug-table sub-components
            ├── RpcConnectionsDebugTable.tsx
            ├── ServicesDebugTable.tsx
            ├── EntitiesDebugTable.tsx
            ├── SessionsDebugTable.tsx
            └── table_styles.ts
```

## The four panels as registrations

`workstationPanelRegistrations` (in `panels/registrations.ts`) is the
array the demos pass to `<PanelViewer panelRegistrations={...} />`.
Each entry is a `PanelRegistration` (the type lives in
`@fxtr-ui/panel-viewer`):

| id (component key) | surface | notes |
|---|---|---|
| `ROOTS_PANEL_COMPONENT` | sidebar button "Roots" | registry browser |
| `WORKSTATION_DEBUG_PANEL_COMPONENT` | sidebar button "Debug" | four resource tables |
| `SESSION_DEBUG_PANEL_COMPONENT` | `forSession` (hidden) | tab title derived from `params.sessionId` |
| `DISPATCHER_DEBUG_PANEL_COMPONENT` | hidden | opened programmatically |

The viewer opens registered panels via `PanelViewerApi.openPanel(id,
params?)` (find-or-open). The workstation-debug panel uses it for both
the sessions-table "open debug" button
(`openPanel(SESSION_DEBUG_PANEL_COMPONENT, { kind: "session_debug",
sessionId })`) and the "Open dispatcher debug" button
(`openPanel(DISPATCHER_DEBUG_PANEL_COMPONENT)`).

## `useWorkstationConnector`

The panel viewer types its `ConnectorContext` against the abstract
`Connector`, but these panels reach `WorkstationConnector`-specific
members — the four `listX({debug?})` resource lists and the raw
`connection` handle (the per-session debug panel opens its own
`registerSubscription` streams). `useWorkstationConnector()`
(`src/useWorkstationConnector.ts`) narrows `useConnector()` to
`WorkstationConnector`; sound because these panels only mount inside a
`<PanelViewer>` given a `WorkstationConnector`.

## Panel notes

- **`RootsPanel`** — stateless `{kind:"roots"}` panel; renders the
  connector's entity + service lists filtered to records with
  `external_names`. Does **not** provide `PanelContext`, so ref clicks
  route through `restoreIntoNewPanel` (open-new) rather than mutating a
  source panel.
- **`WorkstationDebugPanel`** — stateless; subscribes to the four
  `listX({debug:true})` lists and renders one table each. Short ids
  render via `<DebugRefChip>` (no hover dispatch — debug surfaces must
  not track refs in the dispatcher session). No `PanelContext`.
- **`SessionDebugPanel`** — per-session debug surface
  (`{kind:"session_debug", sessionId}`). Attaches to the target session
  via `connector.attachSession` for terminate + lifecycle observation,
  and opens two raw subscriptions via
  `connector.connection.registerSubscription` (`{kind:"session"}` and
  `{kind:"session_debug"}`). Sections: header (+ Force-terminate), last
  state, session pins, open slots, event log. Force-terminate fires
  with no confirmation.
- **`DispatcherDebugPanel`** — reads the dispatcher off
  `<DispatcherProvider>` context and renders `dispatcher.debugView()`
  (defensive empty-state branch for layout restores after `debugView`
  is removed). The "Open dispatcher debug" button in
  `WorkstationDebugPanel` only renders when `dispatcher.debugView !==
  undefined`.

For the generic viewer mechanics (per-panel sessions, navigation,
reflink interaction impls, sidebar, layout persistence, the
registration mechanism, the metadata views, system-type renderers),
see `@fxtr-ui/panel-viewer`'s
[`LOCAL_CODE_OVERVIEW.md`](../fxtr-ui-panel-viewer/LOCAL_CODE_OVERVIEW.md).

## Plans / design notes

See
[`../../plans/phase0/part3_frontend_ui/stage9_workstation_features.md`](../../plans/phase0/part3_frontend_ui/stage9_workstation_features.md)
for the debug-panel / metadata-view / demo-service-renderer plan.
