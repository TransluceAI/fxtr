# `@fxtr-ui/panel-viewer`

The generic, **connector-agnostic** dockview-based panel viewer — the
top-level chrome a fxtr UI drops in. Provides a left-rail open-panels
sidebar, a dockview panel grid, ref-stub dispatch through configurable
type renderers, drag-and-drop link navigation, breadcrumb /
history-root menu inside ref panels, layout persistence through an
injected storage, a connection-status header, hardcoded metadata views,
and the **injected panel-registration** mechanism.

It types against the abstract `Connector` / `Session` from
`@fxtr-ui/core`; a concrete connector (`WorkstationConnector`,
`HttpConnector`, …) flows in at runtime via `<ConnectorProvider>`. The
viewer never imports a connector package.

App-specific panels (roots / debug / custom) are **not** built in —
they are supplied as `PanelRegistration`s. The workstation set lives in
`@fxtr-ui/workstation`; an HTTP demo ships its own.

> **Naming note.** The shell-level symbols are named `PanelViewer` /
> `PanelViewerApi` / `PanelViewerContext` (+ `PanelViewerProvider` /
> `usePanelViewer`). The `@fxtr-ui/workstation-dispatch` package keeps
> its `Workstation`-prefixed exports (`WorkstationNavigateContext`,
> `createWorkstationDispatchRegistry`, …) — those are externalized
> through the dynamic-dispatch import-map, so renaming them is a
> separate, riskier cleanup left as a future TODO.

## Relationship to `@fxtr-ui/workstation-dispatch`

The connector-agnostic **dispatch + ref-rendering layer** lives in the
separate `@fxtr-ui/workstation-dispatch` package (no connector, no
dockview dependency). That package owns the four **dispatch keys**
(`FullPanel` / `BlockLink` / `InlineLink` / `HoverPreview`) + fallback
bundles + `createWorkstationDispatchRegistry`; **`RefLink`**
(presentational) + the **`RefLinkImplContext`**; the **`RefStubResolver`**
context (`entityRef` / `serviceRef` / `classifyShortId`); the
**`WorkstationNavigateContext`**; the **metadata views**
(`EntityMetadataView` / `ServiceMetadataView` / `JsonRefTree`); and the
**dnd** primitives.

This package **provides the concrete impls** those injection contexts
expect:

- `reflink/ShellRefLinkImpl.tsx` — the interaction impl installed into
  `RefLinkImplContext` (composes drag/click/hover/context-menu around a
  dispatched link) **and** the navigation hook installed into
  `WorkstationNavigateContext`;
- `reflink/ShellRefStubResolver.tsx` — the resolver installed into
  `RefStubResolverContext`, backed by the connector's interning +
  `connector.classifyShortId` (delegated — the workstation answers from
  its resource lists, the HTTP connector from its shortId table).

`index.ts` re-exports much of `workstation-dispatch` for convenience.

## Mental model

**Refs are stubs; chain entries carry stubs.** `PanelViewEntry`
(`{stub, display}` from `src/types.ts`) is the in-process shape used
everywhere the viewer holds a navigable target: chain entries,
history-root prefixes, breadcrumb segments, follow-link targets. `stub`
is a connector-interned `EntityRefStub` / `ServiceRefStub`; the session
reducer's `collectStubsInState` walks user state and emits Track ops for
every stub, so anything reachable through the chain or the recursive
history-root prefix is kept alive server-side by the panel session.
`display` selects between the dispatched FullPanel renderer
(`"default"`) and a hardcoded metadata view (`"metadata"`).

Drag payloads cross a `JSON.stringify` boundary (which strips stub class
identity), so they ride a sister `PanelViewDragData = {kind, shortId,
display}`. `setDragData` lowers via `dragDataOfEntry` /
`dragDataOfBreadcrumb`; `DockviewLayout.tsx`'s drop handler lifts via
`entryOfDragData(connector)` / `breadcrumbOfDragData(connector)` before
calling `followLink` / `restoreIntoNewPanel`.

**Per-ref-panel session model.** Every dockview "ref panel"
(`{kind: "ref"}`) owns a `Session`. Its userState carries
`RefPanelChainState = {chain, activeIndex, historyRoot, storages}` — the
chain the user is navigating, the active position, the recursive
history-root recording how the panel was reached, and a per-entry
storage array parallel to `chain`. `PanelWrapper`
(`src/panel/PanelWrapper.tsx`) lifts the userState atom, projects
`storages[activeIndex]` as a single sub-atom, provides `PanelContext`,
and dispatches `FullPanel` (or renders the metadata view) on the active
stub.

**Injected panel registrations.** The viewer owns only the built-in
**ref panel**; every other panel is supplied by the app as a
`PanelRegistration` (`src/shell/PanelRegistration.ts`):

```ts
interface PanelRegistration {
  id: string;                                  // dockview component key
  title: string | ((params) => string);        // derive per-session titles at the registration site
  component: FunctionComponent<IDockviewPanelProps<any>>;
  sidebarButton?: { label: string };           // find-or-open from the sidebar
  forSession?: boolean;                         // per-session variant, keyed <id>-<sessionId>
}
```

- `DockviewLayout` builds its dockview component map from the
  registrations (plus the built-in ref panel) — it imports no concrete
  panels.
- The sidebar renders one find-or-open button per registration with a
  `sidebarButton`.
- `PanelViewerApi.openPanel(id, params?)` opens (or focuses) a
  registered panel programmatically: singletons keyed by `id`,
  `forSession` panels keyed by `<id>-<sessionId>` (sessionId read from
  `params.sessionId`); `params` is passed straight through to the panel
  and to the registration's `title` deriver.
- On an empty boot (no persisted layout) the viewer opens the
  registration named by its **`defaultPanelId`** prop (the workstation
  apps pass `ROOTS_PANEL_COMPONENT`); when omitted it starts blank.

**Viewer-owned ref-panel creation.** `PanelViewerApi.followLink({intent:
"open-new"})` and `restoreIntoNewPanel(seed)` route through `openNewPanel`,
which mints a panel id + `addPanel({sessionId: null})` (mounts pending),
`await connector.createSession(...)`, writes the seed chain state through
a freshly-attached subscription, `await session.detach()` (drains the
seed), then `panel.api.updateParameters({sessionId})` so the panel
attaches via its own subscription. The viewer never holds a long-lived
subscription to a panel's session.

**`PanelSessionRegistry`.** Once a panel's `usePanelSession` reaches
`"live"`, `PanelWrapper` registers the session with a viewer-scoped
`PanelSessionRegistry` (`src/shell/PanelSessionRegistry.tsx`).
Navigation handlers read sessions out of it imperatively (`get`);
reactive consumers (sidebar row labels) subscribe via its `sessions`
atom. Closing a ref panel terminates its session via
`PanelViewerApi.closePanel`.

**Single app-wide `Dispatcher`, supplied by the consumer.** A separate
session backs `@fxtr-ui/react-dispatch`'s state machine. The consumer
owns its lifecycle and passes a pre-built `Dispatcher` as a required
prop; the viewer branches on `dispatcher.status` for loading /
unavailable / connected render gates. The package re-exports
`readDispatcherSessionId` / `writeDispatcherSessionId` /
`clearDispatcherSessionId` for the consumer's persistence.

**Layout persistence.** Through an **injected `storage: Storage`** prop
(no hardcoded `localStorage`) — the workstation demo passes
`localStorage`; the HTTP demo passes the *same* handle as its connector
so sessions + the layout that embeds their ids age together. Two keys:
`fxtr-workstation-layout` (`{version, dockview, sidebar}`, written by
`useLayoutPersistence(storage, …)`) and, separately,
`fxtr-workstation-dispatcher-session-id` (the consumer writes it via the
re-exported `writeDispatcherSessionId(storage, id)` helpers — pass the
same `Storage`). Per-ref-panel session ids ride inside the dockview JSON
via panel params, restored automatically on `fromJSON`.

## Important invariants

- **`onReady` is "once per dockview internal layout," not once per
  page.** In React StrictMode dev mode `DockviewReact` is unmounted +
  remounted, tearing down dockview's internal layout. The handler must
  be idempotent: re-read the persisted layout and replay `fromJSON` on
  every fire, with the default-panel open as a fallback only when both
  there's no persisted layout AND the dockview is empty. Don't
  reintroduce a "did we build the default yet?" flag.
- **Dispatcher `"closed"` is transient, not death.** The consumer's
  `dispatcher.detach()` flips status to `"closed"` locally; only
  `"unavailable"` means the server dropped the session — that's what
  `DispatcherUnavailable` keys on.
- **Metadata-mode entries skip dispatch.** When
  `chain[activeIndex].display === "metadata"`, `PanelWrapper` renders
  `EntityMetadataView` / `ServiceMetadataView` directly instead of
  `RenderDispatch as={FullPanel}`. Both modes share the per-entry
  `storages[activeIndex]` slot, narrowed by each renderer.
- **Roots-style panels that omit `PanelContext` route clicks to
  open-new.** Ref-link interactions branch on `usePanelContext() ===
  null` to choose between `followLink({replace-in-source})` and
  `restoreIntoNewPanel(...)`. App panels that should open links in a new
  panel (e.g. roots, debug surfaces) simply don't provide
  `PanelContext`.

## Directory layout

```
fxtr-ui-panel-viewer/
├── LOCAL_CODE_OVERVIEW.md
├── package.json
├── tsconfig.json
└── src/
    ├── index.ts                   # public surface (+ workstation-dispatch re-exports for convenience)
    ├── types.ts                   # PanelViewEntry (stub-bearing), PanelViewDragData, conversion helpers, StubKind
    ├── breadcrumb/                # chain types + pure transforms + bar/menu UI
    │   ├── BreadcrumbBar.tsx / BreadcrumbEntry.tsx / BreadcrumbSourceContext.tsx
    │   ├── HistoryRootEntry.tsx / HistoryRootMenu.tsx / flatten.ts
    │   ├── history.ts             # compute{Next,OpenNew,Snapshot}* pure fns
    │   └── types.ts               # Breadcrumb, BreadcrumbSource, HistoryRoot, RefPanelChainState
    ├── fallbacks/PanelErrorBoundary.tsx
    ├── panel/                     # ref-panel content renderer + lifecycle
    │   ├── PanelContext.tsx / PanelSessionLifecycle.ts (usePanelSession)
    │   ├── PanelWrapper.tsx       # dockview content renderer for ref panels
    │   ├── RefPanelParams.ts      # {kind:"ref", sessionId} + REF_PANEL_COMPONENT
    │   ├── RefPanelTab.tsx        # custom tab (close X → PanelViewerApi.closePanel)
    │   └── SessionUnavailable.tsx
    ├── persistence/
    │   ├── dispatcher_session_id.ts / layout_v6.ts / useLayoutPersistence.ts
    ├── reflink/                   # concrete impls for workstation-dispatch's injection contexts + interaction hooks
    │   ├── ShellRefLinkImpl.tsx   # RefLinkImpl + WorkstationNavigateContext
    │   ├── ShellRefStubResolver.tsx # RefStubResolver (delegates classifyShortId to the connector)
    │   ├── DebugRefChip.tsx / DraggableRefHandle.tsx / HoverContext.tsx / drag.ts
    │   ├── useDraggable{ChainEntry,HistoryEntry,Ref}.ts
    │   └── useRefClick.ts / useRefContextMenu.tsx / useRefHoverCard.tsx
    ├── shell/                     # viewer + dockview wiring + contexts
    │   ├── ConnectorContext.tsx   # holds the abstract Connector
    │   ├── DockviewLayout.tsx     # registry-driven component map; routes drops
    │   ├── PanelRegistration.ts   # the PanelRegistration type
    │   ├── PanelSessionRegistry.tsx
    │   ├── ShellHeader.tsx        # status dot + URL (ConnectionStatus from @fxtr-ui/core)
    │   ├── PanelViewerApi.ts      # followLink / activateChainEntry / restoreIntoNewPanel / openPanel / closePanel
    │   ├── PanelViewerContext.tsx
    │   └── PanelViewer.tsx   # top-level component (takes connector + dispatcher + panelRegistrations)
    ├── sidebar/                   # open-panels rail (OpenPanelsSidebar + SidebarButton, ResizableSidebar, …)
    ├── system_renderers/          # BasicTypeBadge (FxtrBasicType → "type {name}" badge)
    └── styles/                    # CSS bundle (fw-* classes); consumers import styles/all.css
```

## Module details

### `shell/PanelViewer.tsx`

The top-level component:
`<PanelViewer connector dispatcher panelRegistrations debugUiUrl?
onClearAndReload? />`. Holds the panel-id counter, the panel-session
registry, and the hover-close registry; builds `registrationsById` and
implements `PanelViewerApi` (`followLink`, `activateChainEntry`,
`restoreIntoNewPanel`, `openPanel`, `closePanel`) over the registry +
dockview api ref; derives the sidebar buttons from registrations with a
`sidebarButton`; gates rendering on `dispatcher.status`. `openPanel`
and the empty-boot default-panel open are both registry-driven (no
hardcoded panel kinds).

### `shell/DockviewLayout.tsx`

Registers `PanelWrapper` for ref panels and every `panelRegistration`'s
component from the injected list (no direct panel imports). Routes
`application/x-fxtr-ref` drops through `PanelViewerApi`, deferring to
renderer-owned drop regions via `RefDropRegionContext`.

### `reflink/ShellRefLinkImpl.tsx` + `reflink/ShellRefStubResolver.tsx`

`ShellRefLinkImpl` — the `RefLinkImpl` mounted into `RefLinkImplContext`
(wraps `<RenderDispatch as={BlockLink|InlineLink}>` with the interaction
hooks + a `RefDropRegionProvider`) plus the nav hook installed into
`WorkstationNavigateContext`. `ShellRefStubResolver` — the
`RefStubResolver`: `entityRef`/`serviceRef` delegate to the connector's
intern maps and `classifyShortId` delegates to
`connector.classifyShortId`. Both are abstract-`Connector` members, so
the resolver is connector-agnostic.

### `breadcrumb/history.ts`

Pure transforms over `RefPanelChainState` the `PanelViewerApi` impl
calls: `computeNextChainState`, `computeOpenNewFromLink`,
`computeOpenNewFromChainEntry`, `computeOpenNewFromHistoryEntry`,
`computeSnapshotFromTrail`.

### `system_renderers/BasicTypeBadge.tsx`

Default dispatched renderers for the `FxtrBasicType` system entity:
`BasicTypeBlockLink` / `BasicTypeInlineLink` show a `type {type_name}`
badge. Connector-agnostic. Registered at `BASIC_TYPE_ID_STRING`.

## Tests

Unit tests live in the sibling `@fxtr-ui/panel-viewer-tests` package
(repo convention — a separate `-tests` package, not an in-package
`tests/` dir): `history.test.ts` covers the breadcrumb/history pure
transforms; `persistence.test.ts` round-trips layout +
dispatcher-session-id through an injected `memoryStorage()`. The
registry-driven open paths (`openPanel` / sidebar / `forSession`) are
React+dockview-bound and ride the workstation-demo integration path
rather than a unit harness.

## Plans / design notes

See
[`../../plans/phase0/part3_frontend_ui/stage8_workstation_core_rewrite.md`](../../plans/phase0/part3_frontend_ui/stage8_workstation_core_rewrite.md)
for the panel-shell foundation and
[`../../plans/phase0/part7a_stateless_ws_server/design/06_shell_decoupling.md`](../../plans/phase0/part7a_stateless_ws_server/design/06_shell_decoupling.md)
for the panel-viewer split rationale.
