# Part 4 — PanelViewer refactor (shell decoupling)

Carve the generic, connector-agnostic panel-viewer out of
`@fxtr-ui/workstation` into a new `@fxtr-ui/panel-viewer` package, and
turn the roots/debug panels into **injected, connector-specific
registrations**. Self-contained refactor of existing, working code;
independent of Parts 2/3 but required by the Part 5 demo.

Design rationale + the touchpoint inventory:
[`design/06_shell_decoupling.md`](design/06_shell_decoupling.md).

> **Read the "Correction (2026-06-15)" box at the top of
> [`design/06`](design/06_shell_decoupling.md) first.** This plan was
> originally drafted against a stale `@fxtr-ui/workstation`
> `LOCAL_CODE_OVERVIEW` and over-counts the work: the dispatch keys,
> `RefLink`, the metadata views, the dnd primitives, and the three
> injection contexts (`RefLinkImplContext`, `RefStubResolverContext`
> with `classifyShortId`, `WorkstationNavigateContext`) **already live in
> the connector-agnostic `@fxtr-ui/workstation-dispatch` package** — they
> are *not* moving. What actually moves into `@fxtr-ui/panel-viewer` is
> the **dockview shell mechanics + the concrete reflink interaction
> impls** (`ShellRefLinkImpl` / `ShellRefStubResolver` + the interaction
> hooks). Steps below are corrected to reflect this.

## Re-orientation pointers

| What | Where |
|---|---|
| Shell local overview (refreshed 2026-06-15) | `fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md` |
| Already-generic dispatch layer (**stays put**) | `@fxtr-ui/workstation-dispatch` — keys, `RefLink`, metadata views, dnd, + the `RefLinkImplContext` / `RefStubResolverContext` / `WorkstationNavigateContext` injection points |
| Shell entry (→ `PanelViewer`) | `fxtr-ui-workstation/src/shell/WorkstationShell.tsx` (opens panels + `store`/sessions) |
| Dockview panel registration (fixed map → registry-driven) | `fxtr-ui-workstation/src/shell/DockviewLayout.tsx` |
| Shell mechanics (move to panel-viewer; already connector-agnostic) | `panel/PanelWrapper` + `panel/PanelSessionLifecycle.ts`, `breadcrumb/*`, `sidebar/*`, `WorkstationApi` (→ `PanelViewerApi`: `followLink`/`activateChainEntry`/`restoreIntoNewPanel`) |
| Reflink interaction impls (move to panel-viewer) | `reflink/ShellRefLinkImpl.tsx`, `reflink/ShellRefStubResolver.tsx`, `reflink/{useRefClick,useRefContextMenu,useRefHoverCard,useDraggable*,DebugRefChip,drag,HoverContext}` |
| The four panels (stay in `@fxtr-ui/workstation`, become registrations) | `panels/RootsPanel.tsx`, `panels/WorkstationDebugPanel.tsx`, `panels/SessionDebugPanel.tsx`, `panels/DispatcherDebugPanel.tsx` |
| Ref resolver (already an injected context; `classifyShortId` via lists today) | `reflink/ShellRefStubResolver.tsx` installs `RefStubResolverContext` |
| Layout persistence (hardcoded localStorage, embeds sessionIds) | `persistence/layout_v6.ts`, `persistence/dispatcher_session_id.ts` |
| Header | `shell/ShellHeader.tsx` (`connectionStatus`, `url`) |

## Packaging (resolved — design/06, four layers)

- **`@fxtr-ui/core`** owns the abstract `Connector` + `ConnectionStatus`
  (introduced in Part 3) + `memoryStorage`, alongside the abstract
  `Session` + stubs.
- **`@fxtr-ui/workstation-dispatch`** (already exists, already
  connector-agnostic) — dispatch keys + registry + fallbacks, `RefLink`,
  metadata views, dnd primitives, and the three injection contexts. **No
  change in Part 4.**
- **`@fxtr-ui/panel-viewer`** (new) — the dockview shell mechanics + the
  concrete reflink interaction impls, moved out of `@fxtr-ui/workstation`
  and retyped against the abstract `Connector`. Depends on `core` +
  `workstation-dispatch`.
- **`@fxtr-ui/workstation`** — reduced to the four panels as
  registrations (+ debug tables), depending on `panel-viewer` +
  `connector-workstation` + `workstation-dispatch`. `WorkstationConnector`
  stays in `@fxtr-ui/connector-workstation` (it does *not* move here).
- The Part 5 HTTP demo depends on `@fxtr-ui/panel-viewer` +
  `@fxtr-ui/workstation-dispatch` + its own panels +
  `@fxtr-ui/connector-http`, **never** on `@fxtr-ui/workstation`.

(See design/06's "Naming wart" note: Part 4 renames only the shell-level
symbols that move — `WorkstationShell` → `PanelViewer`, `WorkstationApi`
→ `PanelViewerApi`, `WorkstationContext` → `PanelViewerContext` — and
leaves `workstation-dispatch`'s `Workstation`-prefixed exports alone to
avoid churning the dynamic-dispatch import-map.)

## Steps

### A. Connector conformance (the interface itself is Part 3)
- [x] The abstract **`Connector`** (design/06: `store`, `url`,
  `connectionStatus`, `entityRef`, `serviceRef`, `classifyShortId`,
  `loadEntityData`, `createSession`, `attachSession`, `close`) +
  `ConnectionStatus` are defined in `@fxtr-ui/core` in **Part 3**. No
  `listEntities`/`listServices`/`UiEntityRecord`/`UiServiceRecord`/
  `ReactiveList` on it.
- [x] Declare `WorkstationConnector implements Connector` and add
  `classifyShortId(shortId)` to it — moving the list-backed scan that
  currently lives in `ShellRefStubResolver` onto the connector. Then
  `ShellRefStubResolver` (now in panel-viewer) just delegates to
  `connector.classifyShortId`. (Surfaced a `createSession(metadata)`
  type gap — widened the connector + `CreateSessionRequest.metadata` to
  `JsonValue` to match the abstract surface / `HttpConnector`.)

### B. New `@fxtr-ui/panel-viewer` package — move the shell mechanics
**Not** the dispatch/RefLink/metadata layer — that already lives in
`@fxtr-ui/workstation-dispatch` and stays put. Move from
`@fxtr-ui/workstation`:
- [x] Scaffold the package (workspace member). Move: `PanelWrapper` +
  per-panel session lifecycle (`usePanelSession`, `PanelContext`,
  `RefPanelParams`, `RefPanelTab`, `SessionUnavailable`), chain state +
  `breadcrumb/*` (`computeNextChainState` / `computeOpenNewFromLink` /
  history / bar + menu UI), the `sidebar/*`, `WorkstationApi` +
  `WorkstationContext` / `ConnectorContext` / `PanelSessionRegistry`,
  `ShellHeader`, the dockview grid (`DockviewLayout`), layout
  persistence, `types.ts` (`PanelViewEntry` etc.),
  `fallbacks/PanelErrorBoundary`, and `system_renderers/BasicTypeBadge`.
  (Symbol renames `WorkstationShell`→`PanelViewer` etc. deferred to the
  rename step; the moved symbols keep their names for now.)
- [x] Move the **reflink interaction impls**: `ShellRefLinkImpl` (the
  `RefLinkImpl` + the `WorkstationNavigateContext` nav hook),
  `ShellRefStubResolver` (the `RefStubResolver`, now delegating
  `classifyShortId` to the connector), and the interaction hooks
  (`useRefClick` / `useRefContextMenu` / `useRefHoverCard` /
  `useDraggable*` / `DraggableRefHandle` / `DebugRefChip` / `drag.ts` /
  `HoverContext`). Retyped their `WorkstationConnector`/`WorkstationSession`
  `import type`s to the abstract `Connector`/`Session`. No logic changes.
  (Panels needing concrete-connector members reach them via a
  `useWorkstationConnector()` narrowing hook in `@fxtr-ui/workstation`.)
- [x] `WorkstationShell` (→ `PanelViewer`) takes a `Connector` + a
  `dispatcher` + a `panelRegistrations: PanelRegistration[]` and existing
  props (`debugUiUrl?`, `onClearAndReload?`); installs the
  `workstation-dispatch` injection contexts with the moved impls.
  (The injected `storage: Storage` for layout is the separate Storage
  step below — still `localStorage` for now.)

### C. Panel registration mechanism
- [x] `PanelRegistration { id; title; component;
  sidebarButton?: {label}; forSession?: boolean }` (design/06). (`title`
  is `string | (params) => string` so per-session panels label their tab
  from `params.sessionId` at the registration site.)
- [x] `DockviewLayout` registers components **from the registry**
  instead of importing the panel components directly (its fixed
  component map becomes registry-driven).
- [x] `WorkstationShell`'s (→ `PanelViewer`'s) hard-coded open-panel
  calls become registry-driven: a sidebar button per registration with
  `sidebarButton` (find-or-open), and an `openPanel(id, params?)` API
  exposed to panels for programmatic opens (e.g. workstation-debug →
  per-session debug, using `forSession`). The empty-boot default opens
  the registration named by the viewer's `defaultPanelId` prop (the
  workstation apps pass `ROOTS_PANEL_COMPONENT`). The four
  panels are lifted out of the shell into an exported
  `workstationPanelRegistrations` array the demos pass in (severs
  `WorkstationShell`→panels ahead of the package move).
- [x] Ref resolver: `ShellRefStubResolver` (now in panel-viewer) drops
  its list scan and answers `classifyShortId` via
  `connector.classifyShortId` (+ `entityRef`/`serviceRef`); **no list
  access**. The `RefStubResolverContext` plumbing already exists in
  `workstation-dispatch`; `RefLink` is unchanged (always renders the
  shortId).

### D. Layout persistence through an injected `Storage`
- [x] `layout_v6.ts` + `dispatcher_session_id.ts` take an injected
  **`Storage`** handle (the standard Web Storage interface, design/04)
  instead of hardcoding `localStorage`. No custom storage type — the
  same `Storage` the HTTP connector accepts. `WorkstationShell` gains a
  required `storage: Storage` prop threaded through
  `loadPersistedLayout` / `clearPersistedLayout` / `useLayoutPersistence`.
- [x] App wiring: the **workstation demo** passes a `Storage`
  (`localStorage`) to the `WorkstationShell` (and to the re-exported
  `read/write/clearDispatcherSessionId` helpers) — `WorkstationConnector`
  persists nothing. (The **HTTP demo** passing one shared `Storage` to
  both the connector and the viewer lands with the Part 5 HTTP demo.)

Update CODE_OVERVIEW.md (frontend_lib package list: new
`@fxtr-ui/panel-viewer`; `@fxtr-ui/workstation` reduced to the four panel
registrations) and the LOCAL_CODE_OVERVIEW.md for `@fxtr-ui/workstation`
(+ a new one for `@fxtr-ui/panel-viewer`).

### E. Express the four panels as registrations
The four panels already live in `@fxtr-ui/workstation` — they don't
change packages; they stop being opened by hard-coded shell calls and
instead get expressed as `PanelRegistration`s that the workstation demo
assembles and passes to `PanelViewer`.
- [x] Wrap `RootsPanel` (over `WorkstationConnector.listEntities/
  listServices`), `WorkstationDebugPanel`, `SessionDebugPanel`
  (raw `connection.registerSubscription`), `DispatcherDebugPanel`
  (renders `dispatcher.debugView()` — the Part 1 reload button lives
  inside the dynamic dispatcher's view, so this panel is unchanged) as
  `PanelRegistration`s (`panels/registrations.ts` →
  `workstationPanelRegistrations`). After this, `@fxtr-ui/workstation`'s
  only role is these registrations; the shell mechanics it used to
  contain now live in `@fxtr-ui/panel-viewer`.
- [x] Confirm the sidebar "Roots"/"Debug" buttons + the per-session
  debug open generalize cleanly to `sidebarButton` + `forSession` +
  `openPanel`. (Done in the registration seam: the sidebar derives one
  button per `sidebarButton`; session-debug is a `forSession` panel
  opened via `openPanel(SESSION_DEBUG_PANEL_COMPONENT, { sessionId })`.)

## Tests
- [x] Unit tests follow the repo's **separate `-tests` package**
  convention: added `fxtr-ui-panel-viewer-tests`. Covers the chain/nav
  breadcrumb/history pure transforms (`computeNextChainState` /
  `computeOpenNewFromLink` / `computeOpenNewFromChainEntry` /
  `computeSnapshotFromTrail` — incl. forward/back/truncate, entity-vs-
  service kind, display-mode disambiguation) and layout/dispatcher-id
  persistence round-trips. **Not** covered as unit tests: the
  registry-driven open paths (`openPanel` find-or-open, sidebar
  buttons, `forSession`) — those are React+dockview-bound inside
  `PanelViewer`; they ride the workstation-demo integration path
  instead. (A focused harness — e.g. extracting the panel-id resolution
  into a pure helper — is a sensible follow-up.)
- [x] `classifyShortId` on the workstation connector (list-backed)
  returns correct kinds incl. `null` for unknown / not-yet-loaded
  (`connector.test.ts`). The HTTP connector's table-backed
  `classifyShortId` is covered in its own Part 3 suite.
- [ ] The existing workstation demo + its integration tests pass
  unchanged through the relocated registrations (the real regression
  guard). *Type-checked + the connector unit suites pass; the
  Python-spawning integration suite + manual demo run not exercised in
  this pass — flagged for review.*
- [x] Layout persistence round-trips through an injected storage handle
  (`persistence.test.ts`, via `memoryStorage()`).

Update CODE_OVERVIEW.md (final) + LOCAL_CODE_OVERVIEW.md for both
packages.

## Notes / watch-fors
- This is a refactor of *working* code — keep behavior identical for
  the workstation; the demo + its tests are the safety net. Land it
  in reviewable chunks (mechanics extraction first with the four panels
  still wired, then flip to registrations).
- `ReactiveList` stays in each connector package (workstation +
  http) — it's no longer portable.
- **Naming:** rename only the moved shell-level symbols
  (`WorkstationShell` → `PanelViewer`, `WorkstationApi` →
  `PanelViewerApi`, `WorkstationContext` → `PanelViewerContext`). Do
  **not** rename `@fxtr-ui/workstation-dispatch` or its
  `Workstation`-prefixed exports (`WorkstationNavigateContext`,
  `createWorkstationDispatchRegistry`) — renderer modules import those by
  identity through the dynamic-dispatch import-map (`DEFAULT_EXTERNALS`),
  so a rename there is a separate, riskier cleanup. Left as a future TODO.
- **`system_renderers/BasicTypeBadge`** is connector-agnostic (renders
  the `FxtrBasicType` system entity) — it moves into `@fxtr-ui/panel-viewer`
  with the rest of the mechanics. (Could alternatively live in
  `workstation-dispatch`; pick while coding, but it must not stay behind
  in the reduced `@fxtr-ui/workstation`.)
- The shell's `connector-workstation` coupling is **entirely `import
  type`** (verified 2026-06-15: `WorkstationConnector` / `WorkstationSession`
  / `ConnectionStatus` / record types). Retyping to the abstract
  `Connector`/`Session` is a pure type-swap; the runtime connector already
  flows via `<ConnectorProvider>` context. One nit: `ShellHeader` imports
  `ConnectionStatus` from `connector-workstation` — repoint it to
  `@fxtr-ui/core` (Part 3 moved the type there).
- The **regression safety net** is the existing `@fxtr-ui/workstation-demo`
  + its integration tests — they must pass unchanged through the
  relocated registrations. There are no unit tests inside
  `@fxtr-ui/workstation` itself to port; new unit tests land in
  `@fxtr-ui/panel-viewer`.

## Implementation notes (landed 2026-06-15)

Captured during the build-out (commits `7a.4.2`–`7a.4.8`). Things that
deviated from the plan or that a reviewer should know:

1. **Chunk reorder — `classifyShortId` before the move.** The plan
   ordered the package move (B) before the Storage/`classifyShortId`
   work (D + the resolver half of C). In practice `classifyShortId` had
   to land *first* (commit `7a.4.3`): retyping `ShellRefStubResolver` to
   the abstract `Connector` requires `classifyShortId` to already be a
   `Connector` method, otherwise the resolver still needs
   `WorkstationConnector`'s `listEntities/listServices` and the move
   can't drop the connector-workstation dependency. Same end state, just
   a forced ordering. Storage injection then landed after the move
   (`7a.4.5`).

2. **`implements Connector` surfaced a `createSession` metadata-type
   gap.** Declaring `WorkstationConnector implements Connector` failed
   because `createSession(metadata: Readonly<Record<string, unknown>>)`
   isn't assignable to the abstract `createSession(metadata: JsonValue)`.
   Fixed by widening both `WorkstationConnector.createSession` and the
   wire `CreateSessionRequest.metadata` to `JsonValue` (matching
   `HttpConnector` + the abstract). Consequence: the TS types now permit
   non-object session metadata on the wire; our code never sends that,
   but the Python `/client` side should be assumed to still expect a
   dict — worth a glance if metadata typing ever matters.

3. **Latent bug: `examples/fxtr_workstation_demo/frontend` isn't in the
   root `tsconfig` references**, so `tsc --build` never typechecks it.
   That's why the chunk-1 change (making `panelRegistrations` required)
   silently broke its `<PanelViewer>` mount without tsc complaining;
   fixed by hand in `7a.4.5`. **Follow-up: add that package to the root
   tsconfig `references`** so it can't rot. (Deliberately not done here
   to avoid surfacing unrelated pre-existing errors mid-refactor.)
   → **Resolved post-Part-5:** both it and `@fxtr-ui/agent-plugin-renderer`
   are now in the references + their surfaced errors fixed (see the
   overview's "wire the stragglers into the typecheck graph — DONE").

4. **Registry-driven open paths have no unit harness.** `openPanel`
   (find-or-open, `forSession` keying) + the sidebar buttons live inside
   the `PanelViewer` React component over dockview, so they're only
   exercised through the workstation-demo integration path, not the
   `@fxtr-ui/panel-viewer-tests` unit suite (which covers the pure
   breadcrumb/history transforms + persistence). **Follow-up:** extract
   the panel-id resolution into a pure helper (`(reg, params) =>
   panelId`) so find-or-open + `forSession` keying can be unit-tested
   without rendering.

5. **Empty-boot default is an explicit `defaultPanelId` prop.** The
   viewer opens the registration named by `PanelViewer`'s
   `defaultPanelId` prop on an empty boot (the workstation apps pass
   `ROOTS_PANEL_COMPONENT`); omitting it starts blank. (An earlier
   increment used an implicit "first registration with a `sidebarButton`"
   rule — swapped to the explicit prop so the viewer hardcodes no
   policy.)

6. **`@fxtr-ui/workstation` deps trimmed.** Dropped `@fxtr-ui/react` and
   `@base-ui/react` from its `package.json` (the four panels don't use
   them directly — they flow via `@fxtr-ui/panel-viewer`). Re-add if a
   future panel needs either directly.
