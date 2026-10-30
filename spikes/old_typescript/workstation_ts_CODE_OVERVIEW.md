# fxtr_workstation/ts — Code Overview

TypeScript source for the fxtr-workstation project: an interactive
dockview-style panel viewer over fxtr2 entities. Paired with the
design-system prototype in `reference_info/design-system-prototype.html`
and the fxtr-react / fxtr-react-dispatch substrate in `v2/ts/`.

## Architecture

Each entity panel owns a `{chain, activeIndex, historyRoot}` triple
stored in its dockview params. `chain` is the list of entities the
panel has visited; `activeIndex` is the active position (usually the
last, but back/forward through breadcrumb entries shifts it);
`historyRoot` is a recursive
`{ kind: "namespace" } | { kind: "branched"; prefix; parent }` that
captures how the panel itself was reached (branch from another panel
at its active position, or pin click from the namespace). A fixed
row at the top of each entity panel — `BreadcrumbBar` — renders the
chain with the active entry emphasized and future entries grayed,
plus a history-root icon that opens a `HistoryRootMenu` listing all
ancestor entries across every segment with dividers between
segments.

The shell exposes three navigation methods on `WorkstationApi`,
accessed via `WorkstationContext`:

- `followLink(event)` — content-ref clicks / right-clicks / drag-
  drops. Owns `replace-in-source` chain mutation (via
  `computeNextChainState`) and `open-new` chain derivation (via
  `computeOpenNewFromLink`). Requires a source entity panel.
- `activateChainEntry(panelId, chainIndex)` — cheap shift of an
  existing panel's active position. Used by breadcrumb-bar clicks.
- `restoreIntoNewPanel(params, position?)` — reopen a fully-resolved
  `EntityPanelParams` snapshot in a fresh panel. Used by breadcrumb-
  entry drag/right-click, history-root menu click/drag, and any non-
  panel-origin click/drag (which derive a snapshot via
  `computeSnapshotFromTrail`).

Non-panel origins (namespace pin rows, their hovers, and any future
non-panel surface) are handled by the interaction hooks themselves:
`useEntityClick`, `useEntityContextMenu`, and `useDraggableEntity`
all branch on `usePanelContext()` — if there's no panel context they
route through `restoreIntoNewPanel` / `SnapshotDragPayload` rather
than `followLink`. `NamespacePanel` has no special navigation code.

Drag payloads are discriminated: `LinkDragPayload { kind: "link"; ... }`
carries target + trail + sourcePanelId (drop → followLink), while
`SnapshotDragPayload { kind: "snapshot"; newPanelState; ... }`
carries a pre-computed state (drop → restoreIntoNewPanel).

Layout persistence uses `LAYOUT_STORAGE_VERSION = 3`. Restore
collects entity ids from each panel's `chain` *and* every
`historyRoot` prefix recursively, so breadcrumb labels load without
a cold miss.

Panel persistence is wired so the namespace and FxtrID class
instances don't have to survive dockview's JSON round-trip: the
namespace flows through a `NamespaceContext` and entity panel params
store only strings. On restore, `WorkstationShell` collects every
entity id from the persisted JSON and awaits `tryRetrieveById(id)`
for each (required prop). Unresolved entities still mount;
`EntityPanelWrapper` calls `scope.lift(id)` + `scope.isTracked(id)`
and renders a "Entity not available" body when it comes back false.

`WorkstationShell` composes an `OpenPanelsSidebar` (left rail)
alongside the dockview. The sidebar lists currently-open panels by
dockview group, shows per-row tooltips (base-ui `Tooltip.Provider`
shares the open-delay across the whole list; individual tooltips use
`disableHoverablePopup`), clicks focus, X closes (except for the
namespace panel). A collapse/expand toggle shrinks the sidebar to a
32px rail; when expanded it's wrapped in `ResizableSidebar` so the
user can drag its right edge between 180 and 480px.

The registry is **pure display** — interactive refs live behind a
public `<EntityLink entity as="inline" | "block" />` component.
Dispatch keys are `BlockLink` / `InlineLink` (block + inline link
display), `FullPanel` (entity panel body), and `HoverPreview`
(hover-card body). `LinkProps` is the shared link prop type.
Sidebars and other non-interactive surfaces consume the registry
directly via `<Render as={BlockLink}>`.

See [`plans/phase0/overview.md`](./plans/phase0/overview.md) for the
full design plan.

## Workspace membership

These TS packages are members of the **`v2/ts/` pnpm workspace**, not a
standalone workspace. `pnpm-workspace.yaml` includes
`../../projects/fxtr_workstation/ts/packages/*` alongside its own
`packages/*`. Run `pnpm install`, `pnpm check`, `pnpm lint` from
`v2/ts/` — they cover both trees. Packages physically live here
(instead of under `v2/ts/packages/`) because the broader
`projects/fxtr_workstation/` project is experimental and expected to
grow non-TS code alongside the TS packages; keeping the whole
workstation tree together makes that separation visible.

## Directory layout

```
ts/
├── CODE_OVERVIEW.md                             # this file
├── tsconfig.base.json                           # extends ../../../tsconfig.base.json
├── plans/
│   └── phase0/
│       ├── overview.md                          # phase 0 design plan
│       └── step1_workspace_scaffolding.md       # scaffolding sub-plan
├── reference_info/
│   └── design-system-prototype.html             # CSS token + look source
└── packages/
    ├── fxtr-workstation-theme/                  # @fxtr-workstation/theme (CSS-only)
    │   ├── package.json                         # subpath exports for each .css file
    │   ├── tsconfig.json
    │   ├── src/index.ts                         # empty stub; runtime has no JS
    │   └── styles/
    │       ├── tokens.css                       # design-system `:root` tokens
    │       ├── panel-chrome.css                 # .fw-panel, .fw-panel__body, etc.
    │       ├── fw-dockview-theme.css            # .fw-dockview-theme → dockview --dv-* vars
    │       ├── fonts.css                        # @import Open Sans + JetBrains Mono
    │       ├── hover-card.css                   # .fw-hover-popup (base-ui PreviewCard)
    │       ├── context-menu.css                 # .fw-context-menu-popup / .fw-context-menu-item
    │       ├── sidebar.css                      # .fw-sidebar* classes (open-state width is 100% so ResizableSidebar controls it)
    │       ├── resizable-sidebar.css            # .fw-resizable-sidebar wrapper + .fw-resizable-sidebar__handle
    │       ├── breadcrumb.css                   # .fw-breadcrumb-bar / .fw-breadcrumb-entry / .fw-history-root-menu-popup / .fw-history-root-entry
    │       └── all.css                          # barrel — @imports the above
    ├── fxtr-workstation-core/                   # @fxtr-workstation/core
    │   ├── package.json                         # deps: @fxtr/core, @fxtr/react,
    │   │                                         #   @fxtr/react-dispatch, @fxtr-workstation/theme,
    │   │                                         #   dockview, @base-ui/react; peer: react >=18
    │   ├── tsconfig.json
    │   └── src/
    │       ├── index.ts                         # re-exports below
    │       ├── keys.ts                          # 4 DispatchKey declarations + prop types (FullPanel, BlockLink, InlineLink, HoverPreview, LinkProps, FullPanelProps)
    │       ├── shortId.ts                       # shortId(id) = id.idString().slice(-8)
    │       ├── registry.ts                      # createWorkstationDispatchHandler — thin wrapper around createDispatchRegistry; appends unknownLoadCodec fallbacks. Registry is pure display (no interactions).
    │       ├── EntityLink.tsx                   # <EntityLink entity as="inline"|"block" linkText?> — composes dispatch + 4 interaction hooks. Public component for interactive refs.
    │       ├── DockviewLayout.tsx               # DockviewReact container; registers "entity"+"namespace" panel components; wires drop handling to the workstation API
    │       ├── EntityPanelWrapper.tsx           # dockview panel content renderer; params = { chain, activeIndex, historyRoot }; hydrates active FxtrID via FxtrID.fromIdString(chain[activeIndex]); provides extended PanelContext (chain/active/root); renders BreadcrumbBar above a Suspense+ErrorBoundary-wrapped <Render as={FullPanel}>; scope.lift + isTracked precheck on the active entity
    │       ├── WorkstationShell.tsx             # top-level shell; implements followLink / activateChainEntry / restoreIntoNewPanel; owns hover-close registry, sidebar state, NamespaceContext provider; layout-persistence v3 (collects chain + historyRoot prefixes for tryRetrieveById bridging, then api.fromJSON)
    │       ├── breadcrumb/
    │       │   ├── types.ts                     # Breadcrumb / BreadcrumbSource / BreadcrumbVia + buildTrail; HistoryRoot + EntityPanelParams
    │       │   ├── BreadcrumbSourceContext.tsx  # provider + useBreadcrumbSource
    │       │   ├── history.ts                   # pure transformations: computeNextChainState, computeOpenNewFromLink/ChainEntry/HistoryEntry, computeSnapshotFromTrail, FlatHistoryEntry
    │       │   ├── flatten.ts                   # flattenHistoryRoot(root) → FlatHistoryEntry[] (deepest-first)
    │       │   ├── BreadcrumbBar.tsx            # fixed top-of-panel row; history-root button + chain of entries
    │       │   ├── BreadcrumbEntry.tsx          # one chain entry; click → activateChainEntry, right-click/drag → restoreIntoNewPanel
    │       │   ├── HistoryRootMenu.tsx          # base-ui Menu popup listing flattened ancestor entries grouped by segment
    │       │   └── HistoryRootEntry.tsx         # one menu entry; click/drag → restoreIntoNewPanel(computeOpenNewFromHistoryEntry(...))
    │       ├── workstation/
    │       │   ├── navigate.ts                  # FollowLinkEvent / FollowLinkIntent / DropPosition + WorkstationApi (3 methods)
    │       │   ├── WorkstationContext.tsx       # WorkstationApi context + useWorkstation()
    │       │   ├── PanelContext.tsx             # { panelId, kind, chain, activeIndex, historyRoot } context + usePanelContext()
    │       │   └── NamespaceContext.tsx         # provides FxtrReadNamespace (NamespacePanel reads it via useNamespace)
    │       ├── interactions/
    │       │   ├── drag.ts                      # DRAG_MIME_FXTR_ENTITY + discriminated DragPayload (LinkDragPayload | SnapshotDragPayload) + helpers
    │       │   ├── HoverContext.tsx             # HoverKeptOpenContext + HoverCloseContext (nested-hover keep-open + navigate-driven close-all)
    │       │   ├── useDraggableEntity.ts        # panel-context branch: LinkDragPayload vs SnapshotDragPayload (via computeSnapshotFromTrail)
    │       │   ├── useDraggableChainEntry.ts    # breadcrumb-entry drag-source: SnapshotDragPayload via computeOpenNewFromChainEntry
    │       │   ├── useDraggableHistoryEntry.ts  # history-root-entry drag-source: SnapshotDragPayload via computeOpenNewFromHistoryEntry
    │       │   ├── useEntityClick.ts            # panel-context branch: followLink(replace) vs restoreIntoNewPanel(computeSnapshotFromTrail(...))
    │       │   ├── useEntityHoverCard.tsx       # base-ui PreviewCard wrapper + <Render as={HoverPreview}>; takes `as: "inline"|"block"` so Trigger has a real bounding box
    │       │   ├── useEntityContextMenu.tsx     # panel-context branch; "Open in new panel" → followLink(open-new) or restoreIntoNewPanel(snapshot)
    │       │   └── DraggableEntityHandle.tsx    # drag-only grip for use inside a FullPanel
    │       ├── panels/
    │       │   ├── NamespacePanel.tsx           # listPins() → rows (pin name + <EntityLink as="block">); namespace from useNamespace(), params are empty
    │       │   └── namespacePanelComponent.ts   # PANEL_COMPONENT_NAMESPACE / NAMESPACE_PANEL_ID constants
    │       ├── sidebar/
    │       │   ├── OpenPanelsSidebar.tsx        # left rail: groups of rows; click-focus; X-close (suppressed for namespace-main); collapse/expand toggle (32px rail when collapsed)
    │       │   ├── ResizableSidebar.tsx         # controlled (width + onWidthChange) wrapper with a drag-to-resize right edge; ported from thingwatcher
    │       │   ├── usePanelLayout.ts            # useSyncExternalStore over dockview api events → SidebarGroup[]; reconstructs FxtrID from entityIdString
    │       │   └── SidebarTooltip.tsx           # base-ui Tooltip wrapper (disableHoverablePopup) + Tooltip.Provider re-export (shared delay across rows)
    │       ├── components/
    │       │   └── JsonTree.tsx                 # FxtrCbor-aware recursive tree; tagged IDs → <EntityLink as="inline">
    │       └── fallbacks/
    │           ├── FullPanelFallback.tsx        # loads UnknownEntity, renders JsonTree(raw_cbor)
    │           ├── BlockLinkFallback.tsx        # labeled "link" pill (pure display; interactions come from EntityLink at use sites)
    │           ├── InlineLinkFallback.tsx       # inline <code> shortId (pure display)
    │           ├── HoverPreviewFallback.tsx     # compact two-line card
    │           └── PanelErrorBoundary.tsx       # React error boundary wrapping each entity panel's <Render>; renders a themed error body on crash
    ├── example-demo-fingerprinting-workstation/ # @fxtr-workstation/example-demo-fingerprinting
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── vite.config.ts
    │   ├── index.html
    │   └── src/main.tsx                         # createRoot stub
    └── example-dev-workstation/                 # @fxtr-workstation/example-dev (dev demo)
        ├── package.json                         # scripts: serve (tsx src/server.ts), dev (vite), build
        ├── tsconfig.json
        ├── vite.config.ts
        ├── index.html
        └── src/
            ├── entities.ts                      # DemoNote, DemoSection, DemoArchive, DemoListNode (self-referential via bicodec.lazy)
            ├── server.ts                        # Node http + Hono + vite middleware on :3460; pins demo/{section,first-note,archive,chain-head}; seeds 20-node DemoListNode chain
            └── client/
                ├── main.tsx                     # vite entry; imports dockview + theme CSS
                ├── App.tsx                      # connects HTTP namespace, mounts WorkstationShell (namespace panel is the entry point)
                └── handlers.tsx                 # DemoNote FullPanel/BlockLink/HoverPreview + DemoSection FullPanel (items as EntityLinks) + DemoSection HoverPreview (with nested EntityLink) + DemoListNode FullPanel/BlockLink/HoverPreview (HoverPreview embeds a nested EntityLink to `next`); DemoArchive intentionally unregistered
```

## Packages

- **`@fxtr-workstation/theme`** — CSS-only. Ships eight stylesheets
  plus a barrel:
  - `tokens.css` — design tokens in `:root` (spacing, colors,
    typography, radii, panel-chrome heights), ported verbatim from
    the design-system prototype.
  - `panel-chrome.css` — `.fw-panel`, `.fw-panel__header`,
    `.fw-panel__body`, etc. Utility classes for workstation surfaces
    that aren't dockview panels (hover cards, namespace panel body,
    nested sub-cards). Prefixed `fw-` to avoid colliding with host
    styles.
  - `fw-dockview-theme.css` — a single `.fw-dockview-theme` class
    defining the ~30 `--dv-*` CSS variables dockview reads. Also
    sets `font-family: var(--font-prose)` so dockview's tab titles
    don't fall back to the browser default. workstation-core applies
    this class to the dockview container *instead of* any built-in
    `.dockview-theme-*` class.
  - `fonts.css` — Google Fonts `@import` for Open Sans + JetBrains
    Mono.
  - `hover-card.css` — `.fw-hover-popup` for the base-ui PreviewCard
    popup rendered by `useEntityHoverCard`. Also reused by
    `SidebarTooltip`.
  - `context-menu.css` — `.fw-context-menu-popup` / `.fw-context-menu-item`
    for the base-ui ContextMenu popup rendered by `useEntityContextMenu`.
  - `sidebar.css` — `.fw-sidebar*` classes for `OpenPanelsSidebar`:
    container (`.fw-sidebar--open` is `width: 100%` — the wrapping
    `ResizableSidebar` owns the actual width), header, groups, rows,
    close buttons, and the collapse toggle.
  - `resizable-sidebar.css` — `.fw-resizable-sidebar` wrapper +
    `.fw-resizable-sidebar__handle`; the handle is a 4px right-edge
    strip that shows `var(--accent)` on hover.
  - `all.css` — imports all eight above in the right order. Consumers
    wire it up at their entry point with
    `import "@fxtr-workstation/theme/all.css"` (and separately import
    dockview's own stylesheet, which this barrel intentionally
    doesn't bundle).
- **`@fxtr-workstation/core`** — the app library. Responsibilities:
  - **Dispatch keys** (`keys.ts`): `FullPanel`, `BlockLink`,
    `InlineLink`, `HoverPreview` + their prop types (`FullPanelProps`,
    `LinkProps`).
  - **Registry** (`registry.ts`): `createWorkstationDispatchHandler`
    is a thin wrapper over `createDispatchRegistry` that appends
    `unknownLoadCodec` fallback entries. The registry is **pure
    display** — it does not attach interactions to link dispatch
    entries. Consumers that want interactive refs use `<EntityLink>`;
    non-interactive surfaces (e.g. the sidebar) render
    `<Render as={BlockLink}>` directly.
  - **`EntityLink`** (`EntityLink.tsx`): public wrapper component.
    Takes `entity: FxtrID`, `as: "inline" | "block"`, and any
    `LinkProps` (e.g. `linkText`); composes the four interaction
    hooks (hover card, context menu, click, draggable) around
    `<Render as={BlockLink | InlineLink}>`. Consumers with unusual
    layouts can compose the individual hooks directly.
  - **Fallbacks** (`fallbacks/`): `FullPanelFallback` renders
    `JsonTree(raw_cbor)`; the three link/preview fallbacks are
    compact stubs keyed off `shortId`. Pure display — interactions
    come from `<EntityLink>` at consumer use sites.
  - **JsonTree** (`components/JsonTree.tsx`): FxtrCbor-aware recursive
    viewer that renders tagged ID classes as `<EntityLink as="inline">`.
  - **Navigation core** (`workstation/`):
    - `WorkstationApi` (defined in `navigate.ts`, accessed via
      `useWorkstation()` from `WorkstationContext`) exposes three
      methods: `followLink(event)` for content-ref interactions in
      a panel context (owns `replace-in-source` / `open-new`
      branching), `activateChainEntry(panelId, chainIndex)` for
      in-place breadcrumb shifts, and `restoreIntoNewPanel(params,
      position?)` for reopening a fully-resolved
      `EntityPanelParams` snapshot in a fresh panel.
    - `PanelContext` (only provided by `EntityPanelWrapper`) tells
      descendants which panel they live in — `panelId`, `kind`,
      `chain`, `activeIndex`, `historyRoot`. Interaction hooks
      branch on `usePanelContext()`: panel-context callers use
      `followLink` / `LinkDragPayload`; callers without one (e.g.
      namespace pin rows) compute a snapshot via
      `computeSnapshotFromTrail` and route through
      `restoreIntoNewPanel` / `SnapshotDragPayload`.
  - **Breadcrumb threading** (`breadcrumb/`): `BreadcrumbSource` context
    mirrors thingwatcher; `EntityPanelWrapper` and `useEntityHoverCard`
    nest providers so a click deep inside stacked hover cards emits a
    full chain of `{ from, to, via }` segments. `buildTrail(source,
    target)` is the helper that assembles the final trail at each
    navigation point. The `history.ts` module contains the pure
    transformations the shell applies: `computeNextChainState`
    (replace-in-source), `computeOpenNewFromLink` /
    `computeOpenNewFromChainEntry` / `computeOpenNewFromHistoryEntry`
    (new-panel derivation from various origins), and
    `computeSnapshotFromTrail` (non-panel-origin snapshot
    derivation). `BreadcrumbBar` renders the chain across the top of
    each entity panel; `HistoryRootMenu` opens from the history-root
    icon and lists flattened ancestor entries grouped by segment.
  - **Interactions** (`interactions/`):
    - `drag.ts`: `application/x-fxtr-entity` MIME + discriminated
      `DragPayload` union. `LinkDragPayload { kind: "link"; ... }`
      carries `entityIdString` + `trail` + optional `sourcePanelId`
      (drops route through `followLink`).
      `SnapshotDragPayload { kind: "snapshot"; ... }` carries a
      fully-resolved `newPanelState: EntityPanelParams` (drops route
      through `restoreIntoNewPanel`). Payloads also write a
      `text/plain` fallback with the target id string.
    - `HoverContext.tsx`: `HoverKeptOpenContext` (nested hovers keep
      parents open) + `HoverCloseContext` (navigate closes all hovers).
    - Hooks (`useDraggableEntity`, `useDraggableChainEntry`,
      `useDraggableHistoryEntry`, `useEntityClick`,
      `useEntityHoverCard`, `useEntityContextMenu`) — composable
      building blocks used by `EntityLink` / `BreadcrumbEntry` /
      `HistoryRootEntry` and reachable directly for custom layouts.
    - `DraggableEntityHandle`: drag-only grip for use inside a
      `FullPanel` renderer (e.g. a title bar).
  - **Panel content renderers**:
    - `EntityPanelWrapper` (entity panel). Reads `{ chain,
      activeIndex, historyRoot }` from params and hydrates the
      active `FxtrID` via `FxtrID.fromIdString(chain[activeIndex])`
      — params are JSON-safe so dockview's `toJSON`/`fromJSON`
      persistence round-trip is lossless. Before rendering, calls
      `scope.lift(entity)` (walks parent scopes to bridge the
      entity if reachable) + `scope.isTracked(entity)` as a
      precheck: tracked entities render through a
      `PanelErrorBoundary` + `Suspense` wrapping `<Render
      as={FullPanel}>`; untracked entities render a friendly "Entity
      not available" body. Renders `BreadcrumbBar` as a fixed row
      above the body. Implements the `setPanelTitle` contract;
      provides `PanelContext` + `BreadcrumbSource`. Keys the inner
      `<Render>` on the active entity id string so
      `api.updateParameters({ ... })` causes a clean remount (this
      is what makes click-replace-in-place work).
    - `NamespacePanel` (panels/): shows `namespace.listPins()` as a
      two-column list (pin name + `<EntityLink as="block">`). Reads
      the namespace via `useNamespace()` rather than params, and its
      own `NamespacePanelParams` are `{}`. Doesn't provide
      `PanelContext`, so interactions on pin rows route through
      `restoreIntoNewPanel` / `SnapshotDragPayload`.
  - **Namespace context** (`workstation/NamespaceContext.tsx`): simple
    provider / `useNamespace()` hook the shell wraps the subtree in
    so rich class instances don't have to flow through dockview
    params.
  - **Sidebar** (`sidebar/`):
    - `OpenPanelsSidebar` — left-rail listing open panels grouped
      by dockview group; click focuses, X closes (suppressed for
      namespace-main), collapse/expand toggle. When open it's wrapped
      by `ResizableSidebar`; when collapsed it renders a 32px rail
      with just the expand toggle.
    - `ResizableSidebar` — controlled (`width` + `onWidthChange`)
      wrapper with a draggable right-edge handle. Ported from
      thingwatcher; hover color uses `var(--accent)`. The shell
      persists the width into the layout payload.
    - `usePanelLayout(api)` — `useSyncExternalStore` subscription
      over dockview's add/remove/active/layout events → flat
      `SidebarGroup[]` with entity FxtrIDs reconstructed from each
      panel's active entity id string.
    - `SidebarTooltip` — base-ui `Tooltip.Root disableHoverablePopup`
      wrapper that renders `<Render as={HoverPreview}>` on the right
      of a row. Re-exports `Tooltip.Provider` as
      `SidebarTooltipProvider` so the sidebar can wrap its row list
      in one to share the open-delay across tooltips.
  - **Shell** (`WorkstationShell.tsx`): owns the dockview api, the
    panel-id counter (`entity-<n>` — no dedup), the hover-close
    registry, the `WorkstationApi` implementation (the three
    `followLink` / `activateChainEntry` / `restoreIntoNewPanel`
    methods), the sidebar's collapse/expand + width state, the
    `NamespaceContext` provider, and the layout-persistence loop.
    - Opens the namespace panel on mount (canonical id
      `namespace-main`) and auto-reopens it if closed.
    - Accepts `{ namespace, tryRetrieveById, initialPanels? }`.
      `initialPanels` is only honoured when there is no persisted
      layout to restore. `tryRetrieveById` is required (layout
      persistence is always on); the dev demo supplies
      `(id) => ns.tryRetrieveById(id)` from `HttpFxtrReadNamespace`.
    - Persists layout + `sidebarCollapsed` + `sidebarWidth` to
      `localStorage["fxtr-workstation-layout"]` (versioned via
      `LAYOUT_STORAGE_VERSION = 3`; 200ms debounced via
      `onDidLayoutChange`). On mount, if a layout was persisted,
      the shell collects every entity id from each panel's `chain`
      and every `historyRoot` prefix recursively, and awaits
      `tryRetrieveById(FxtrID.fromIdString(s))` for each
      (best-effort: null results and thrown errors are logged but
      don't fail the restore). Then it calls
      `api.fromJSON(persisted.dockview)` straight — no layout
      mutation. Entities that didn't bridge mount as panels whose
      `scope.isTracked(entity)` is false, and
      `EntityPanelWrapper`'s "Entity not available" body handles
      them. On any parse/schema/fromJSON failure the key is cleared
      and the default layout (namespace-only) is built.
      `setApi` / `apiRef.current` only fire after this settles, so
      drag-drops during the restore window are no-ops. After
      restoring, bumps the entity-id counter past the largest
      `entity-<n>` in the saved layout so newly-added panels don't
      collide.
- **`@fxtr-workstation/example-demo-fingerprinting`** — package
  scaffolded for the fingerprinting demo workstation; `src/main.tsx`
  is currently a `createRoot` stub.
- **`@fxtr-workstation/example-dev`** — dev demo. A self-contained
  server + client exercising the whole stack. Four entity types:
  - `DemoNote` — handlers for `FullPanel`, `BlockLink`,
    `HoverPreview`. `InlineLink` falls back.
  - `DemoSection` — handlers for `FullPanel` (lists `items` as
    interactive `<EntityLink as="block">`) and `HoverPreview` (embeds
    a nested `<EntityLink as="block">` for the section's first item
    so the nested-hover-keep-open path is exercisable live).
  - `DemoArchive` — no handlers; exercises the JsonTree `FullPanel`
    fallback with nested section ids rendered via
    `<EntityLink as="inline">` wrapping the `InlineLink` fallback.
  - `DemoListNode` — self-referential linked list (via
    `bicodec.lazy`), seeded 20-deep with `demo/chain-head` pinned so
    you can walk the chain and scrub back and forth through the
    breadcrumb trail. `HoverPreview` embeds an `<EntityLink>` to
    `next` to exercise nested-hover drag paths.

  Server pins: `demo/section`, `demo/first-note`, `demo/archive`,
  `demo/chain-head`. Run with
  `pnpm --filter @fxtr-workstation/example-dev serve` (server +
  vite dev middleware on `http://localhost:3460`). The namespace panel
  is the entry point; the demo root div sets
  `fontFamily: var(--font-prose)` + `color: var(--text-primary)` so
  the header inherits the prose font.

## Tooling

- TypeScript: per-package `tsconfig.json` extends
  `../../tsconfig.base.json` here, which extends v2's
  `tsconfig.base.json`. All packages are `composite: true` (inherited)
  and build via `tsc --build` from `v2/ts/`.
  - Workstation package tsconfigs do **not** list v2 packages in
    `references`. This works today because every `@fxtr/*` package's
    `exports` point at `./src/index.ts`, so tsc reads v2 source
    directly through pnpm's symlinks rather than consuming emitted
    `.d.ts`. Revisit if we flip `@fxtr/*` package `exports` to
    `./dist/...` (e.g. when publishing v2 packages) — at that point
    cross-boundary project references become load-bearing for build
    ordering and emitted `.d.ts` correctness.
- Biome: linted/formatted from `v2/ts/` alongside v2 packages via the
  `package.json` lint scripts.
- Vite: demo-package only; `pnpm --filter @fxtr-workstation/example-demo-fingerprinting dev`.
- No vitest yet — added when there's logic worth testing.

## Workspace note

The TypeScript packages under `ts/packages/` are members of the `v2/ts/` pnpm workspace (not a standalone workspace here). They depend on `@fxtr/core`, `@fxtr/react`, and `@fxtr/react-dispatch` from `v2/ts/packages/`, and we share v2's lockfile / install / `pnpm check` / `pnpm lint`. Run those commands from `v2/ts/`, not from here. We keep the packages physically under `projects/fxtr_workstation/` (rather than `v2/ts/packages/`) because this project is experimental and is expected to grow non-TS code alongside the TS packages — keeping the whole workstation tree in one place makes that separation visible.
