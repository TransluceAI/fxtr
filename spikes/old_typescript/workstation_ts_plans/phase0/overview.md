# Phase 0 — fxtr-workstation overview

Broad-strokes plan for a new panel-based interactive viewer over fxtr2
entities. This phase 0 doc sketches the pieces and how they fit together;
each implementation step will get its own sub-plan alongside this file.

## Goal

Build an interactive dockview-style panel viewer whose unit of content is an
**fxtr2 entity** (not thingwatcher's "thing"). Three existing bodies of code
inform the design — we take a slice from each:

| Source | Take | Skip |
| --- | --- | --- |
| `spikes/thingwatcher/thingwatcher/frontend/` | dockview layout, hover preview cards, drag-to-open, right-click context menu, the four-renderer taxonomy | graphql layer, graph visualization, timeline / ticks, esbuild-wasm dynamic rendering, shadow-root style isolation |
| `v2/ts/packages/fxtr-react/`, `v2/ts/packages/fxtr-react-dispatch/` | `FxtrProvider` + `useFxtrLoad` for data loading; `DispatchProvider` + `<Render>` for type→renderer dispatch | nothing — use both as-is |
| `projects/fxtr_workstation/ts/reference_info/design-system-prototype.html` | CSS variable token system (spacing, colors, typography, radii); overall light/blue aesthetic; compact panel chrome | exact grid layout — dockview owns the layout |

Think of workstation as an **alternative** to `v2/ts/packages/fxtr-reports-react`: same data-loading + dispatch substrate, different shell + interaction model (panels instead of a scroll-with-sidebar report).

## Package layout

Mirrors the `v2/ts/` workspace structure. Inside
`projects/fxtr_workstation/ts/`:

```
pnpm-workspace.yaml               # packages/*
package.json                       # root workspace + scripts (build / lint / test)
tsconfig.base.json                 # shared compiler options
tsconfig.json                      # project references
biome.json                         # lint/format
CODE_OVERVIEW.md                   # current-state summary
reference_info/
  design-system-prototype.html     # source-of-truth for tokens
plans/
  phase0/overview.md               # this file
packages/
  fxtr-workstation-core/           # app library
  fxtr-workstation-theme/          # CSS tokens + panel chrome
  example-demo-fingerprinting-workstation/  # demo client
```

Package dependencies:

- `fxtr-workstation-theme` — zero deps; pure CSS files.
- `fxtr-workstation-core` — depends on `@fxtr/core`, `@fxtr/react`,
  `@fxtr/react-dispatch`, `dockview`, `@base-ui/react` (for the context
  menu + preview card primitives thingwatcher uses), and
  `fxtr-workstation-theme`.
- `example-demo-fingerprinting-workstation` — depends on
  `fxtr-workstation-core`, `@fxtr/http-client`, and reuses entity
  definitions from `example-demo-fingerprinting-report`.

## Dispatch keys

Declared once in `fxtr-workstation-core/src/keys.ts`. Four keys, paralleling
thingwatcher's renderer taxonomy:

| Key | Role | Props beyond `entityRef` |
| --- | --- | --- |
| `FullPanel` | Contents of a dockview panel for this entity — the "main" view. | `{ navState?: unknown; setPanelTitle: (title: ReactNode) => void }` |
| `BlockRef` | Block-level reference rendered inside another entity's `FullPanel`, inside the open-panels sidebar, or as a namespace-panel row. Fills its container width. | `{ linkText?: string }` |
| `InlineRef` | Inline reference flowing with prose inside another entity's `FullPanel`. | `{ linkText?: string }` |
| `HoverPreview` | Compact card shown when hovering any ref. Read-only, no navigation controls. | `{}` |

Handlers register with `createDispatchRegistry([entry(key, codec, Component), ...])` from fxtr-react-dispatch. Fallback components (for entity types with no registered renderer) ship in workstation-core — one per key, taking `entityId` only and rendering a simple "#shortId typeId" stub.

### `FullPanel` panel-title contract

`setPanelTitle` must be safe to call during render (including the first
render). The panel-wrapper component owns a title state initialized to a
default of `"#shortId typeName"` (read synchronously from the loaded
`UnknownEntity`) and exposes a stable setter. Calls made during render are
batched into a `useEffect` that pushes the new title into the dockview
panel API; calls made after mount update immediately. Accepting a
`ReactNode` (not just a string) lets renderers include e.g. a small
type-coloured dot or a monospace id.

## Runtime shape

Top-level React tree a consumer mounts:

```
<FxtrProvider scope={namespace.scope}>
  <DispatchProvider handler={workstationHandler}>
    <WorkstationShell
      initialPins={pins}
      onBreadcrumb={(b) => console.log(b)}  // default: console.log
    />
  </DispatchProvider>
</FxtrProvider>
```

`WorkstationShell` composition:

```
+------------------------------------------------------+
| Header (title + sidebar-toggle)                      |
+----------+-------------------------------------------+
| Open-    |  DockviewReact                            |
| panels   |    ├── Namespace panel (pin list)         |
| sidebar  |    └── <FullPanel entity> panels...       |
+----------+-------------------------------------------+
```

Two distinct surfaces for *already-open panels* vs *the namespace*:

- **Open-panels sidebar** — `ResizableSidebar` on the left, showing the
  dockview panels currently open. Each entry renders the panel's entity via
  `<Render entity={id} as={BlockRef} />` with no pin-name prefix (an open
  panel may point at an entity that has no pin — it was reached through
  another entity's internals). Click focuses the panel, X closes it.
  Non-entity panels (e.g. the Namespace panel itself) appear with just
  their panel title. This is the direct analog of thingwatcher's
  `components/Sidebar/PanelSidebar.tsx`.
- **Namespace panel** — a regular dockview panel (not a sidebar) opened by
  default. It's like thingwatcher's `RootsPanel` but full-sized, showing
  `ns.listPins()` as a two-column list: the pin name on the left, a
  `<Render entity={id} as={BlockRef} />` on the right. Because the pin
  name is meaningful here (bigger surface, user is browsing the
  namespace), we show it; the sidebar doesn't. The Namespace panel
  auto-reopens if closed (like `MAIN_PANEL_IDS` in thingwatcher).
- **Entity panels** — one per opened entity, keyed by
  `entity.id.idString()`. The panel wrapper is a thin component that calls
  `<Render entity={id} as={FullPanel} setPanelTitle={...} />`.

## Renderer conventions

Renderers are ordinary React components that receive `entityRef: EntityRef<T>` plus any key-specific props. They load data with `useFxtrLoad(entityRef)` and recurse into nested refs with `<Render entity={...} as={BlockRef|InlineRef} />`.

No shadow DOM isolation; no custom hooks taxonomy (like thingwatcher's `usePayload` / `useLinks` / handle factory). All renderers live in the same React tree and style themselves with CSS modules or tokens from workstation-theme.

Two separate interaction helpers, mirroring thingwatcher's split between
`NodeRef` (a whole ref) and `DraggableIDMarker` (a grip inside a larger
view):

- **`<EntityRefRoot entityId>`** — wraps the root of a `BlockRef` or
  `InlineRef` renderer's output. Adds: drag source (MIME payload = entity
  id), click → open-in-panel + emit breadcrumb, hover trigger for
  `<Render entity={id} as={HoverPreview} />` in a base-ui `PreviewCard`,
  and a base-ui `ContextMenu` offering "Open in panel". A ref *is* the
  hover / click / drag target, so the whole root gets wired up.
- **`<DraggableEntityHandle entityId>`** — for use *inside* a `FullPanel`
  renderer to mark a specific element (e.g. a title or id badge) as the
  drag grip for opening the entity elsewhere. No hover preview (the entity
  is already fully visible). No context menu in phase 0; we may add one
  later if useful.

Both are thin wrappers that compose smaller building blocks
(`useDraggableEntity`, `useEntityHoverCard`, `useEntityContextMenu`) that
can be used directly by renderers with unusual layouts.

## Interactions — what to port and from where

Thingwatcher files we mirror against entity ids (not base64 keys):

| Behavior | Thingwatcher source | Workstation destination |
| --- | --- | --- |
| Drag handle → drag data | `frontend/lib/DraggableIDMarker.tsx`, `frontend/lib/dragConstants.ts` | `src/interactions/drag.tsx` |
| Hover preview cards (nested, close-on-navigate) | `frontend/lib/HoverPreview.tsx`, `frontend/lib/HoverContext.tsx` | `src/interactions/hoverPreview.tsx` |
| Right-click menu | `frontend/components/ThingContextMenu/ThingContextMenu.tsx` | `src/interactions/contextMenu.tsx` |
| Accept drops into dockview to open panels | `frontend/components/DockviewLayout/DockviewLayout.tsx` (onDidDrop + onUnhandledDragOverEvent) | `src/shell/DockviewLayout.tsx` |
| Sidebar listing open panels | `frontend/components/Sidebar/PanelSidebar.tsx`, `usePanelLayout.ts` | `src/shell/OpenPanelsSidebar.tsx` — direct port; renders `BlockRef` for entity panels, plain title for others |
| Namespace / pins list panel | `frontend/components/RootsPanel/RootsPanel.tsx` (structure) | `src/shell/NamespacePanel.tsx` — pin name + `BlockRef` per row; auto-reopens |

MIME payload carries `entityId.idString()` plus any pending-breadcrumb data. No tick state, no b64 key.

## Breadcrumb callback

Navigation events emit structured breadcrumbs through a context callback. No
UI renders them in phase 0 — the default callback is `console.log`.

```ts
type Breadcrumb = {
  fromEntityId?: string;   // idString of source entity, if any
  toEntityId: string;      // idString of destination
  via: "panel-ref" | "hover-ref" | "preview-ref" | "namespace-panel" | "open-panels-sidebar";
};
```

Emitted from: ref click inside a `FullPanel`, ref click inside a
`HoverPreview`, click on a row in the Namespace panel, click on an entry
in the open-panels sidebar. The callback is provider-level so we can
later swap `console.log` for a real breadcrumb card without re-threading
context.

## Theme / design tokens

`fxtr-workstation-theme/` ships:

- **`tokens.css`** — the `:root { ... }` block from
  `design-system-prototype.html`: `--sp-*`, `--ramp-*`, `--surface-*`,
  `--text-*`, `--accent-*`, `--status-*`, fonts, text sizes, radii.
- **`panel-chrome.css`** — `.panel`, `.panel__header`, `.panel__title`,
  `.panel__body` styles for use inside dockview panel contents.
- **`dockview-overrides.css`** — targets dockview's CSS custom properties so
  the panel frames / tabs / separators use our tokens. Panel tabs don't have
  to look identical to the HTML prototype; we just want the palette to
  match.
- **`fonts.css`** — optional: imports Open Sans + JetBrains Mono from Google
  Fonts, same as the prototype.

`fxtr-workstation-core` imports `tokens.css` + `panel-chrome.css` +
`dockview-overrides.css` at its entrypoint. Renderers use tokens directly
via `var(--accent)` etc. — no shadow-DOM isolation.

## Layout persistence

Dockview layout serialized to `localStorage` under
`fxtr-workstation-dockview-layout`, mirroring thingwatcher's approach
(`DockviewLayout.tsx` `onDidLayoutChange` + `fromJSON`/`toJSON`). The
Namespace panel is re-added on restore if missing (the same
`MAIN_PANEL_IDS` pattern thingwatcher uses). Closed entity panels stay
closed. No URL sync — panel state is too complex to serialize into a URL
cleanly. Future: a JSON export/import UI for sharing layouts.

## Demo — `example-demo-fingerprinting-workstation`

Points at the same HTTP server (`pnpm serve <data-dir>`) that
`example-demo-fingerprinting-report` runs. Reuses entity definitions from
that package (`src/entities.ts`): `CategoryAggregate`, `RubricText`,
`Transcript`, `FingerprintedTranscript`, `FingerprintGraderResult`,
`SimulatorBio`.

Minimum scope: one `FullPanel` + one `BlockRef` renderer per entity type,
lifting presentational bits (e.g. `MessageBubbles`) from the report demo
where convenient. `HoverPreview` and `InlineRef` can fall back to
workstation-core's defaults initially.

The entry point boots the namespace, discovers pins (`reports/*`), and
mounts `WorkstationShell` with those pin IDs. The user then drags, clicks,
and right-clicks their way through the data.

## Future work (not phase 0)

1. **Dynamic rendering** — compile-at-load pipeline where servers ship TSX
   source for renderers, compiled client-side into a dispatch handler. In
   thingwatcher this lives across several files, all worth reading before
   porting:
   - `frontend/components/DynamicRenderer/esbuildSetup.ts` — esbuild-wasm
     init, plugins for virtual module injection, dialect validation,
     compiling a TSX bundle into a `CompiledModule`.
   - `frontend/components/DynamicRenderer/rendererCache.ts` — content-addressed cache of compiled modules.
   - `frontend/components/DynamicRenderer/DynamicRenderer.tsx` — runtime
     wrapper (fetches the module via GraphQL, renders with an error
     boundary, wraps in a shadow root).
   - `frontend/components/DynamicRenderer/FallbackRenderers.tsx` — default
     renderers when compilation fails or the type isn't registered.
   - `frontend/lib/ThingWatcherBindingsExternal.tsx` — the SDK module
     injected at compile time as the bare import `thingwatcher-bindings`.
     This is the public surface server-side renderers see.
   - `frontend/lib/ThingRenderLib.tsx` — the internal (non-shadow-slotted)
     versions of the same components, used by app code.
   - `frontend/lib/handleFactory.ts`, `frontend/lib/rendererHooks.ts` — the
     handle-based hook system (`usePayload`, `useLinks`,
     `usePersistentThingState`, ...).
   - `frontend/lib/ShadowDOM.tsx` — shadow-root / slot helpers; required if
     dynamic renderers need style isolation.
   - `thingwatcher/thingwatcher-bindings.d.ts` — type definitions matching
     `ThingWatcherBindingsExternal.tsx` for server-side type-checking of
     renderers.
   Port strategy: produce a `DispatchHandler` whose `lookup` fetches TSX for
   a given `(key, typeId)` pair, compiles it, and returns a resolution
   whose `Component` takes `entityRef` and uses fxtr hooks. The SDK module
   injected for renderers (`workstation-bindings`) would re-export
   `useFxtrLoad`, `<Render>`, `EntityInteractiveRoot`, the dispatch-key
   symbols, etc.

2. **Visible breadcrumb UI** — a collapsible breadcrumb card attached to
   `FullPanel` showing how the user arrived, populated via the same
   callback we'll already be emitting.

3. **Panel layout export/import** — a command to download/upload the
   current dockview state as JSON.

4. **Persistent per-entity renderer state** — port thingwatcher's
   `persistentStateStore.ts` if/when renderers want local state that
   survives unmount (e.g. expanded sections in a log viewer).

## Implementation phases

Each of these will get its own plan doc in `plans/phase0/` when we reach
it. Order:

- [x] 1. Workspace scaffolding — pnpm workspace, root package.json,
      tsconfig.base, biome config, empty package dirs.
      → See `step1_workspace_scaffolding.md`. Workstation packages
      piggy-back on v2/ts's pnpm workspace.
- [x] 2. `fxtr-workstation-theme` — port tokens + panel chrome + dockview
      overrides + fonts. → See `step2_theme_package.md`. Ships five
      stylesheets (tokens, panel-chrome, fw-dockview-theme, fonts,
      all) via subpath exports. Dockview theme authored from scratch
      (approach A): `.fw-dockview-theme` maps all ~30 `--dv-*` vars.
- [x] 3. `fxtr-workstation-core` MVP — dispatch key declarations,
      `WorkstationShell` with a single hardcoded `FullPanel` panel,
      fallback renderers, theme imports.
      → See `step3_workstation_core_mvp.md`. Exercised end-to-end by a
      new `@fxtr-workstation/example-dev` package (server + vite on
      :3460) with `DemoNote` (registered) and `DemoSection`
      (unregistered — exercises JsonTree FullPanel fallback).
- [x] 4. Interactions — `EntityRefRoot`, `DraggableEntityHandle`, hover
      preview cards, right-click context menu, dockview drop handling.
      Namespace panel pulled forward into step 4. Navigation funnels
      through a single `WorkstationShell.navigate` function with
      `replace-in-source` / `open-new` intents; every panel gets a
      unique id. → See `step4_interactions.md`.
- [x] 5. Open-panels sidebar + layout persistence. Also pulled in a
      naming refactor: the dispatch registry is now pure display,
      interactive refs move to a new public `<EntityLink>` component,
      and the dispatch keys / prop types rename `Ref` → `Link` to avoid
      overloading fxtr's own `EntityRef`. Namespace panel already
      landed in step 4. → See `step5_open_panels_sidebar.md`.
- [x] 5b. Layout-restore fix + ResizableSidebar. Panel params stop
      carrying class instances (namespace via `NamespaceContext`,
      entity panels via `{ entityIdString }`); `WorkstationShell`
      bridges restored entities into the scope through a required
      `tryRetrieveById` prop before calling `api.fromJSON`; sidebar
      width is now draggable. → See
      `step5b_restore_fix_and_resizable_sidebar.md`.
- [x] 6. Per-panel breadcrumb history. Each entity panel owns
      `{chain, activeIndex, historyRoot}`; `navigate` split into
      `followLink` / `activateChainEntry` / `restoreIntoNewPanel`;
      `BreadcrumbBar` + `HistoryRootMenu` render the chain and
      ancestor entries. Drag payloads discriminated
      `LinkDragPayload | SnapshotDragPayload`. Non-panel origins
      (namespace pin rows) go through `restoreIntoNewPanel` directly.
      `LAYOUT_STORAGE_VERSION` bumped to 3; restore walks chain +
      historyRoot prefixes. Dev demo adds a 20-node `DemoListNode`
      linked list pinned at `demo/chain-head`.
      → See `step6_breadcrumbs.md`.
- [ ] 7. `example-demo-fingerprinting-workstation` — handlers for the six
      fingerprinting entity types, wired to the existing server.
- [ ] 8. Update `projects/fxtr_workstation/ts/CODE_OVERVIEW.md` and any
      `LOCAL_CODE_OVERVIEW.md` files with the final shape.
