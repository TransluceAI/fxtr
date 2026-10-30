# Step 4 — Interactions + Namespace panel

Make refs interactive (click / drag / hover / right-click), wire dockview
to accept drops, and land the Namespace panel as the workstation's default
landing surface.

After step 4 the shell can open and juggle multiple panels, the namespace
panel is the entry point, and every ref renderer (including the built-in
fallbacks) is clickable / hoverable / draggable / context-menuable with no
per-renderer setup.

## Decisions (resolved in pre-plan discussion)

1. **Auto-wrap `BlockRef` / `InlineRef` entries in `EntityRefRoot`** inside
   `createWorkstationDispatchHandler`. `HoverPreview` is **not** wrapped —
   hovers-within-hovers happen naturally via `useEntityHoverCard` on any
   inner refs, not on the popup itself. Consumers who want bare refs can
   drop down to `createDispatchRegistry`.
2. **`WorkstationShell` API pivots.** Drops the single `entity` prop;
   accepts `namespace: FxtrReadNamespace` and optional
   `initialPanels: FxtrID[]`. Owns the central `navigate(event)` function
   and provides it via a new `WorkstationContext`.
3. **Every opened panel is unique.** Panel ids are counter-based
   (`panel-1`, `panel-2`, …); nothing is derived from the entity id. Two
   panels can display the same entity and live independently. This
   naturally sets up future per-panel breadcrumb-history tracking.
4. **Click on a ref replaces the source panel in place** via
   `panel.api.updateParameters({ entity })`. If the click didn't originate
   in an entity panel (namespace, future sidebar), it degrades to
   open-new.
5. **Drag / context-menu always open a new panel.** Drops pass their drop
   position through to the new panel's placement.
6. **Namespace panel is a first-class dockview panel** (new component id
   `namespace`). It auto-opens on first mount and auto-reopens if closed
   (mirrors thingwatcher's `MAIN_PANEL_IDS` pattern, though we only have
   one such panel for now).
7. **Open-panels sidebar and layout persistence stay in step 5.**
8. **Full hover-context port.** `HoverKeptOpenContext` (nested hovers keep
   parents open) and `HoverCloseContext` (navigation closes all hovers)
   mirror thingwatcher.
9. **Breadcrumb trail is threaded end-to-end via a single `navigate()`
   call.** See [Breadcrumb & navigation model](#breadcrumb--navigation-model).
   No separate `onBreadcrumb` callback. For step 4, the shell's default
   `navigate` implementation just `console.log`s the trail alongside
   doing the panel work.
10. **Theme additions:** `hover-card.css` + `context-menu.css`.

## New files in `fxtr-workstation-core/src/`

```
src/
├── breadcrumb/
│   ├── types.ts                     # Breadcrumb, BreadcrumbVia, BreadcrumbSource
│   └── BreadcrumbSourceContext.tsx  # provider + useBreadcrumbSource()
├── workstation/
│   ├── WorkstationContext.tsx       # { navigate } context + useWorkstation()
│   ├── PanelContext.tsx             # { panelId, kind } context + usePanelContext()
│   └── navigate.ts                  # NavigateEvent type + helpers
├── interactions/
│   ├── drag.ts                      # MIME + DragPayload JSON + set/parse helpers
│   ├── HoverContext.tsx             # HoverKeptOpenContext + HoverCloseContext
│   ├── useDraggableEntity.ts        # onDragStart → setDragData (id + trail + sourcePanelId)
│   ├── useEntityClick.ts            # onClick → navigate({ intent: "replace-in-source" })
│   ├── useEntityHoverCard.tsx       # returns wrap component (base-ui PreviewCard)
│   ├── useEntityContextMenu.tsx     # returns wrap component (base-ui ContextMenu)
│   ├── EntityRefRoot.tsx            # composes hover + context menu + click + draggable
│   └── DraggableEntityHandle.tsx    # composes only useDraggableEntity
├── panels/
│   ├── NamespacePanel.tsx           # dockview IDockviewPanelProps<{ namespace }> content
│   └── namespacePanelComponent.ts   # PANEL_COMPONENT_NAMESPACE constant + id helpers
├── DockviewLayout.tsx               # UPDATED: register namespace panel + drop handling
├── EntityPanelWrapper.tsx           # UPDATED: provide PanelContext + BreadcrumbSource
├── WorkstationShell.tsx             # REWRITTEN: multi-panel, navigate(), provides contexts
├── registry.ts                      # UPDATED: auto-wrap BlockRef/InlineRef entries
└── index.ts                         # add new exports
```

Theme additions:

```
fxtr-workstation-theme/styles/
├── hover-card.css                   # .fw-hover-popup
└── context-menu.css                 # .fw-context-menu-popup, .fw-context-menu-item
```

`all.css` updated to import them.

## Breadcrumb & navigation model

### `Breadcrumb`

Trail segment produced at each navigation step (hover / click / drop).

```ts
type BreadcrumbVia =
  | "panel-ref"        // ref inside an entity panel
  | "hover-ref"        // ref inside a hover preview card
  | "namespace-panel"  // row click in the namespace panel
  | "context-menu";    // right-click → "Open in panel"

interface Breadcrumb {
  fromEntityId?: string; // idString of source entity; absent for entry-point taps
  toEntityId: string;
  via: BreadcrumbVia;
}
```

### `BreadcrumbSource` context

Mirrors thingwatcher's `useBreadcrumbSource`. Tracks the trail being
assembled by hover nesting.

```ts
interface BreadcrumbSource {
  pendingBreadcrumbs: Breadcrumb[];
  currentKey: FxtrID | null; // null means "no from-segment" (namespace row)
  via: BreadcrumbVia;        // what type the next appended segment should be
}
```

Providers:
- `EntityPanelWrapper` → `{ pending: [], currentKey: entity, via: "panel-ref" }`.
- `useEntityHoverCard`'s popup → extends parent chain with
  `{ from: parent.currentKey, to: hoveredEntity, via: parent.via }`,
  new `currentKey = hoveredEntity`, new `via = "hover-ref"`.
- `NamespacePanel` row wrapper → `{ pending: [], currentKey: null, via: "namespace-panel" }`.

When a click or drag fires at a ref to `target`:
- If `source.currentKey` is non-null, append
  `{ from: source.currentKey, to: target, via: source.via }` to `pending`.
- If `source.currentKey` is null, emit a length-1 trail
  `[{ to: target, via: source.via }]` (no `fromEntityId`).

Drag-start bakes the **final** segment into the payload, so by the time a
drop fires, `payload.trail` is the complete trail. This keeps click and
drop paths symmetric.

### `NavigateEvent`

Produced at the interaction site, consumed by `navigate()`.

```ts
type DropPosition = {
  referenceGroup?: DockviewGroupPanel;
  direction?: "left" | "right" | "above" | "below";
};

type NavigateIntent =
  | { kind: "replace-in-source" }
  | { kind: "open-new"; position?: DropPosition };

interface NavigateEvent {
  target: FxtrID;
  trail: Breadcrumb[];
  sourcePanelId?: string;
  intent: NavigateIntent;
}
```

### `WorkstationContextValue`

```ts
interface WorkstationContextValue {
  navigate(event: NavigateEvent): void;
}
```

### `WorkstationShell.navigate` — step 4 implementation

```
function navigate(event):
  switch event.intent.kind:
    case "replace-in-source":
      const src = event.sourcePanelId && api.getPanel(event.sourcePanelId)
      if src && sourceIsEntityPanel(src):
        src.api.updateParameters({ entity: event.target })
        src.api.setActive()
      else:
        // no valid source to replace → degrade to open-new
        addPanel(event.target)
    case "open-new":
      addPanel(event.target, event.intent.position)
  console.log("[workstation] navigate trail:", event.trail, "source:", event.sourcePanelId)
```

Panel creation: `id = "panel-" + nextCounter()`, component `"entity"`,
params `{ entity: event.target }`, position per `open-new.position` or
next to the currently-focused group otherwise.

The `sourceIsEntityPanel` check just inspects the panel's component id.
Future panel kinds declare their own component ids and can be treated
accordingly.

### How this extends later (not step 4)

When we want per-panel breadcrumb history:
- `WorkstationShell` keeps a `Map<panelId, Breadcrumb[]>`.
- `replace-in-source` appends `event.trail` to source panel's history
  before updating params.
- `open-new` seeds new panel's history with `event.trail`.
- Back-navigation becomes `{ kind: "back-in-source" }` — no changes at
  interaction call-sites.

## Interaction components — contracts

### `interactions/drag.ts`

```ts
const DRAG_MIME_FXTR_ENTITY = "application/x-fxtr-entity";

interface DragPayload {
  entityIdString: string;
  trail: Breadcrumb[];            // final segment already appended
  sourcePanelId?: string;
}
```

Helpers: `setDragData(dt, payload)`, `parseDragData(dt): DragPayload | null`,
`hasDragData(dt)`. Also sets a `text/plain` fallback with the idString.
Skip per-type / per-key MIME variants — no custom drop regions in step 4.

### `useDraggableEntity(entity)`

```ts
function useDraggableEntity(entity: FxtrID): {
  draggable: true;
  onDragStart: (e: React.DragEvent) => void;
};
```

Reads `BreadcrumbSource` + `PanelContext`. `onDragStart`:
1. Builds the complete trail (pending + final segment).
2. Packs a `DragPayload` with `entityIdString`, `trail`,
   `sourcePanelId: panelContext?.panelId`.
3. `setDragData(e.dataTransfer, payload)`.

### `useEntityClick(entity)`

```ts
function useEntityClick(entity: FxtrID): {
  onClick: (e: React.MouseEvent) => void;
};
```

Reads `BreadcrumbSource`, `PanelContext`, `WorkstationContext`,
`HoverCloseContext`. On click:
1. `e.preventDefault(); e.stopPropagation();`
2. `closeAllHovers()`.
3. `navigate({ target: entity, trail, sourcePanelId, intent: { kind: "replace-in-source" } })`.

### `useEntityHoverCard(entity, as)` → `<Wrap>` component

Uses base-ui's `PreviewCard.Root / Trigger / Portal / Positioner / Popup`.
Delays: `delay={500}`, `closeDelay={300}` (same as thingwatcher).

**Trigger element matters.** base-ui's Positioner reads
`getBoundingClientRect()` off the Trigger element to anchor the
popup, so the trigger must have a real box. We pass `render` with a
real element matching the ref kind:
- `as === "inline"` → `<span style={{ display: "inline-block" }} />`
- `as === "block"` → `<div style={{ display: "block" }} />`

`EntityRefRoot` threads its own `as` prop through. (An earlier
`display: contents` variant anchored the popup at `{0,0}` — see
follow-up #4.)

Popup contents:
- `<HoverKeptOpenContext.Provider value={setKeptOpenByChild}>` so nested
  hovers keep this one open.
- `<BreadcrumbSourceProvider value={extendedChain}>` with
  `via: "hover-ref"` on the nested source.
- `<Suspense fallback={<div className="fw-hover-popup-loading">…</div>}>`
  `<Render entity={entity} as={HoverPreview} />`.

Popup class: `fw-hover-popup`.

Registers a close-listener with `HoverCloseContext` so `closeAllHovers()`
snaps it shut (navigation elsewhere should close hover previews). Uses
`useSetKeptOpen()` to notify parent cards when we're open.

### `useEntityContextMenu(entity)` → `<Wrap>` component

Uses base-ui's `ContextMenu.Root / Trigger / Portal / Positioner / Popup /
Item`. Single item "Open in panel" whose handler mirrors
`useEntityClick` but emits `intent: { kind: "open-new" }` and via
`"context-menu"`.

Popup class `fw-context-menu-popup`, items `fw-context-menu-item`.

### `<EntityRefRoot entity={id} as="inline"|"block">`

Props: `entity: FxtrID`, `as?: "inline" | "block"` (default `"block"`),
`children: ReactNode`. Composes:
- `useEntityHoverCard(entity)` wrap (outermost).
- `useEntityContextMenu(entity)` wrap.
- Element (span for inline, div for block) with `onClick` from
  `useEntityClick`, plus `draggable` + `onDragStart` from
  `useDraggableEntity`, plus a `title` tooltip ("Click to navigate,
  right-click for menu, drag to open in new panel").

### `<DraggableEntityHandle entity={id}>`

Minimal — just a `<span>` with `draggable` + `onDragStart` from
`useDraggableEntity`. No hover / no context menu / no click.
Grip-style cursor via inline style.

## Registry auto-wrap

In `registry.ts`, `createWorkstationDispatchHandler` maps entries:

```
for each entry:
  if entry.key === BlockRef:
    entry.Component = withRefInteractions(entry.Component, { as: "block" })
  else if entry.key === InlineRef:
    entry.Component = withRefInteractions(entry.Component, { as: "inline" })
```

```ts
function withRefInteractions<T extends Entity, P>(
  Inner: ComponentType<{ entityRef: EntityRef<T> } & P>,
  opts: { as: "inline" | "block" },
): ComponentType<{ entityRef: EntityRef<T> } & P> {
  return (props) => (
    <EntityRefRoot entity={props.entityRef.id} as={opts.as}>
      <Inner {...props} />
    </EntityRefRoot>
  );
}
```

Applied to both user entries and the built-in fallbacks
(`BlockRefFallback`, `InlineRefFallback`). `FullPanel` and `HoverPreview`
entries are not wrapped.

## Panel pieces

### `PanelContext`

```ts
interface PanelContextValue {
  panelId: string;
  kind: "entity";  // later: "namespace", "trail", ...
}
```

Provided by `EntityPanelWrapper`:

```tsx
<PanelContext.Provider value={{ panelId: props.api.id, kind: "entity" }}>
  <BreadcrumbSourceProvider value={{ pending: [], currentKey: entity, via: "panel-ref" }}>
    <Render entity={entity} as={FullPanel} setPanelTitle={setPanelTitle} />
  </BreadcrumbSourceProvider>
</PanelContext.Provider>
```

### `EntityPanelWrapper` — update

- Reads `props.params.entity` each render (unchanged).
- Wraps render in `PanelContext.Provider` + `BreadcrumbSourceProvider` as
  shown above.
- `setPanelTitle` contract unchanged.
- On param change (via `updateParameters`), dockview re-renders; we rely
  on that. If dockview's behavior on `updateParameters` isn't a clean
  prop change, we'll bump a `key={entity.idString()}` on the inner
  `<Render>` to force a remount. (Verify during implementation.)

### `NamespacePanel`

Dockview content renderer with params `{ namespace: FxtrReadNamespace }`.

- On mount, calls `namespace.listPins()` (async — cached after first
  resolution).
- Renders a scrollable table:

  ```
  ┌─ Namespace ─────────────────────────────┐
  │ demo/section        <Render as=BlockRef/>│
  │ demo/first-note     <Render as=BlockRef/>│
  │ …                                        │
  └──────────────────────────────────────────┘
  ```

- Each row wraps in `BreadcrumbSourceProvider` with
  `{ pending: [], currentKey: null, via: "namespace-panel" }` — so a
  click on the inner BlockRef produces a length-1 trail
  `[{ to: id, via: "namespace-panel" }]`.
- Does **not** provide a `PanelContext`. This is the mechanism that
  causes clicks-from-namespace to open new panels (sourcePanelId stays
  undefined).

Panel component id: `PANEL_COMPONENT_NAMESPACE = "namespace"`.
Canonical panel id: `"namespace-main"` (only one instance at a time).

### `DockviewLayout` — update

- Add `namespace` to the components map.
- Pass `onDidDrop` + wire `onUnhandledDragOverEvent` via the `onReady`
  event (api subscriptions).
- `onUnhandledDragOverEvent`: accept if
  `dt.types.includes(DRAG_MIME_FXTR_ENTITY)`.
- `onDidDrop`: parse payload, call
  `workstation.navigate({ ..., intent: { kind: "open-new", position } })`.
  `position` built from `positionToDirection(event.position)` +
  `event.group`.

Needs access to `workstation.navigate`; easiest path is to accept it as a
prop (wired by `WorkstationShell`) rather than using context inside the
handler.

### `WorkstationShell` — rewrite

```ts
interface WorkstationShellProps {
  namespace: FxtrReadNamespace;
  initialPanels?: FxtrID[];
}
```

Internals:
- `apiRef: DockviewApi | null`.
- `counterRef: number` for unique panel ids.
- Ready handler:
  - Always open the namespace panel
    (`id: "namespace-main"`, `component: "namespace"`,
    `params: { namespace }`).
  - For each `initialPanels[i]`, open an entity panel to the right of
    namespace.
  - Subscribe `api.onDidRemovePanel`: if `id === "namespace-main"`,
    schedule re-add in next tick.
  - Subscribe `api.onUnhandledDragOverEvent` + `onDidDrop` wired to
    `navigate`.
- Memoize `navigate` per the algorithm above. Exposes it via
  `WorkstationContext` and also passes directly to `DockviewLayout` for
  drop wiring.
- Provides `HoverCloseContext` so all hover cards can register and be
  closed on navigation. `navigate` calls `closeAllHovers()` at the top.

Top-level tree:

```tsx
<WorkstationContext.Provider value={{ navigate }}>
  <HoverCloseContext.Provider value={hoverClose}>
    <DockviewLayout onReady={onReady} navigate={navigate} />
  </HoverCloseContext.Provider>
</WorkstationContext.Provider>
```

Layout persistence: **still deferred to step 5**.

## Theme

### `hover-card.css`

Targets real tokens from `tokens.css`; `fw-hover-popup-loading` is a
small muted sibling class used by the Suspense fallback.

```
.fw-hover-popup {
  background: var(--surface-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: 0 2px 8px rgba(15, 20, 40, 0.12);
  padding: var(--sp-4);
  max-width: 360px;
  min-width: 180px;
  color: var(--text-primary);
  font-family: var(--font-prose);
  font-size: var(--text-body-sm);
  line-height: var(--lh-compact);
  z-index: 1000;
}
```

### `context-menu.css`

```
.fw-context-menu-popup {
  background: var(--surface-card, #fff);
  border: 1px solid var(--surface-line, #d8d8d8);
  border-radius: var(--radius-sm, 4px);
  padding: 4px 0;
  min-width: 160px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  z-index: 1000;
  font-size: 13px;
}

.fw-context-menu-item {
  padding: 6px 12px;
  cursor: pointer;
  color: var(--text-body, #222);
  outline: none;
  transition: background-color 0.1s;
}

.fw-context-menu-item[data-highlighted],
.fw-context-menu-item:focus {
  background-color: var(--surface-alt, #f3f3f3);
}
```

Both imported from `all.css`.

## Demo updates (`example-dev-workstation`)

Handlers registered on the demo client (see `src/client/handlers.tsx`):

- `DemoNote`: `FullPanel`, `BlockRef`, `HoverPreview`. `InlineRef`
  falls back.
- `DemoSection`: `FullPanel` (lists `section.items` as interactive
  `<Render as={BlockRef}>`) + `HoverPreview` (embeds a nested
  `<Render as={BlockRef}>` pointing at the section's first item —
  hook for exercising nested hover cards).
- `DemoArchive`: **no handlers at all.** Exercises the JsonTree
  FullPanel fallback, with the nested section ids in the CBOR tree
  rendered through the auto-wrapped interactive InlineRef fallback.

Entity defs and server pins:
- `DemoArchive = { name, sections: array(entityRef(DemoSection)) }`
  added alongside the existing `DemoNote` / `DemoSection`.
- Server pins: `demo/section`, `demo/first-note`, and new
  `demo/archive` (populated with one section pointing at the
  existing section).

`App.tsx`: drop the toolbar + `entity` toggle. Mount becomes:

```tsx
<WorkstationShell namespace={state.ns} />
```

The namespace panel is the entry point; user drags/clicks from
there. Header stays for branding; the root div sets
`fontFamily: var(--font-prose)` + `color: var(--text-primary)` so
the prose font cascades into it.

## Implementation order

Roughly dependency order:

- [x] Theme: `hover-card.css`, `context-menu.css`; update `all.css`.
- [x] `breadcrumb/types.ts`, `breadcrumb/BreadcrumbSourceContext.tsx`.
- [x] `workstation/PanelContext.tsx`.
- [x] `workstation/navigate.ts` (types) + `workstation/WorkstationContext.tsx`.
- [x] `interactions/drag.ts`.
- [x] `interactions/HoverContext.tsx`.
- [x] `interactions/useDraggableEntity.ts`, `useEntityClick.ts`.
- [x] `interactions/useEntityHoverCard.tsx`, `useEntityContextMenu.tsx`.
- [x] `interactions/EntityRefRoot.tsx`, `DraggableEntityHandle.tsx`.
- [x] Update `registry.ts`: auto-wrap BlockRef/InlineRef entries (incl.
      fallbacks). (Renamed to `registry.tsx` since it now contains JSX.)
- [x] Update `EntityPanelWrapper.tsx`: provide PanelContext +
      BreadcrumbSource. Keyed the inner `<Render>` on `entity.idString()`
      so `api.updateParameters({ entity })` forces a clean remount.
- [x] `panels/NamespacePanel.tsx` + `panels/namespacePanelComponent.ts`.
- [x] Update `DockviewLayout.tsx`: register namespace panel, wire drop
      handling, accept `navigate` prop.
- [x] Rewrite `WorkstationShell.tsx`: multi-panel + default namespace +
      provide all contexts.
- [x] Update `src/index.ts` with new exports.
- [x] Demo: add `DemoSection` FullPanel handler.
- [x] Demo: add `DemoSection` HoverPreview handler with nested
      `<Render as={BlockRef}>` for testing nested hover cards.
- [x] Demo: add `DemoArchive` entity + server pin.
- [x] Demo: simplify `App.tsx` to mount with just `namespace`;
      set `fontFamily: var(--font-prose)` on the root so demo text
      inherits the prose font.
- [x] Theme: set `font-family: var(--font-prose)` on
      `.fw-dockview-theme` so dockview tab titles use Open Sans.
- [x] Align `BlockRefFallback` / `InlineRefFallback` tokens with
      real `tokens.css` names so "NOTE" and "REF" pills match in
      size/shape.
- [x] Thread `as` through `useEntityHoverCard` +
      `EntityRefRoot` so PreviewCard.Trigger has a real box —
      popups now anchor off the ref, not the document origin.
- [x] Simplify `EntityPanelWrapper` title handling — seed
      `titleRef.current = shortId(entity)` directly on entity change;
      effect is just `api.setTitle(titleRef.current)`.
- [x] `pnpm check` + `pnpm lint` clean (run from `v2/ts/`).
- [x] Smoke-tested via curl: `/pins` returns all three pins including
      `demo/archive`; HTML + `/src/client/main.tsx` +
      `/src/client/handlers.tsx` return 200. **Full visual smoke test
      (hover positioning / click-replace-in-source / drag / drop /
      context menu / nested hover / close-on-navigate / auto-reopen
      namespace panel) done by user; hover preview positioning
      confirmed fixed.**
- [x] Update `projects/fxtr_workstation/ts/CODE_OVERVIEW.md` with step 4
      additions.

## Out of scope (still)

- Open-panels sidebar — step 5.
- Layout persistence to localStorage — step 5.
- Per-panel breadcrumb-history state / back-navigation intent —
  post-step-4 (the data threading is set up, but nothing stores or
  displays history yet).
- Visible breadcrumb UI — phase 1.
- Dynamic rendering — phase 1.
- ThingDropRegion analog (custom in-renderer drop targets).

## Follow-ups / risks surfaced during implementation

1. **`panel.api.updateParameters` + inner remount.** Dockview's
   `updateParameters` updates params but reuses React component
   instances, so renderer-local state would survive a click-to-
   navigate swap. Keyed the inner `<Render>` in `EntityPanelWrapper`
   on `entity.idString()` so dockview-driven param changes force a
   clean unmount/mount of the dispatched renderer.
2. **`EntityPanelWrapper` title slot.** The wrapper seeds
   `titleRef.current = shortId(entity)` directly on each entity
   change (tracked via a `lastEntityRef` inline check in render), so
   a renderer that never calls `setPanelTitle` shows the default
   rather than the previous entity's stale title. Effect path is
   now a single `api.setTitle(titleRef.current)` with no null case.
3. **`HoverPreview` dispatch-key type.** Initially declared with
   `Record<string, never>` as props; this makes
   `<Render entity={id} as={HoverPreview} />` fail to type-check
   because `RenderProps<K>` intersects with the extra-props type and
   `Record<string, never>` rejects anything non-empty. Switched to
   `declareDispatchKey("HoverPreview")` (default `object`) — wide enough
   that the intersection types cleanly. If we ever want to restrict
   HoverPreview to strictly no extra props, fxtr-react-dispatch would
   need to special-case empty-object keys in `RenderProps`.
4. **PreviewCard positioning and `display: contents`.** Initially the
   `PreviewCard.Trigger` was rendered via
   `render={<span style={{ display: "contents" }} />}` — the popup
   anchored at `{0,0,0,0}` because an element with `display: contents`
   has no box for `getBoundingClientRect`. Switched
   `useEntityHoverCard` to take an `as: "inline" | "block"` arg and
   render a real `<span style={{display:"inline-block"}}>` or
   `<div style={{display:"block"}}>`. `EntityRefRoot` threads `as`
   through. `ContextMenu.Trigger` still uses `display: contents`
   because right-click menus position at the cursor, not off the
   trigger's rect.
5. **`exactOptionalPropertyTypes: true` everywhere.** Every optional
   field in the public types (`sourcePanelId`, `DropPosition.*`,
   `NavigateIntent.position`) is declared with explicit `| undefined`
   so call sites can pass `?:` values through without conditional
   spreads. Some internal assemblies (in `useDraggableEntity`,
   `useEntityClick`, `useEntityContextMenu`, `DockviewLayout` drop
   handler) still use conditional spreads to avoid inserting
   `undefined` into exact-optional fields.
6. **Font-family not inherited from nowhere.** Dockview doesn't set
   its own font-family, so tab titles fell back to the browser
   default. Added `font-family: var(--font-prose)` to
   `.fw-dockview-theme`. The demo `App.tsx` root now also sets
   `fontFamily: var(--font-prose)` + `color: var(--text-primary)` so
   the demo header inherits. Host apps that mount
   `WorkstationShell` should do the same at some ancestor.
7. **Fallback ref styling alignment.** `BlockRefFallback` and
   `InlineRefFallback` previously used made-up tokens
   (`--surface-alt`, `--surface-line`, `--radius-sm`, `--text-body`,
   hardcoded 9px/12px) that don't exist in `tokens.css`; they worked
   via fallbacks but looked different from user-registered BlockRefs.
   Repointed both at real tokens (`--surface-raised`,
   `--border-subtle`, `--radius-badge`, `--text-caption`,
   `--text-body-sm`, `--font-prose`, `--font-mono`) so "NOTE" and
   "REF" pills match in size and shape.
8. **Counter-based panel ids** — simple but not persistence-friendly
   (the counter resets on reload). Fine for step 4 since layout
   persistence is step 5; revisit there. Using `namespace-main` as the
   canonical id for the namespace panel means any future persisted
   layout that contains an entity panel with that id would collide —
   unlikely, but worth noting.
9. **Drop-over-empty-dockview behaviour.** `onUnhandledDragOverEvent`
   fires regardless of whether the cursor is over a group; the handler
   lets `event.group ?? undefined` pass through to dockview. When no
   reference group is present (truly empty area) dockview places the
   panel in a new group; visually confirmed this pattern works in
   thingwatcher, but not exercised in our smoke test. Flag for user
   smoke testing.
10. **`FxtrID` reconstruction at drop.** Used `FxtrID.fromIdString`
    from `@fxtr/core`; confirmed it round-trips for all tag kinds we
    care about (`d`/`c`/`n`/`p`/`u`). If we later allow dropping
    builtin (`!`) or ground (`.`) ids, the dispatch-render path will
    throw per its contentless-id guard — we just log and no-op on
    parse failure.
11. **Namespace panel + scope warming.** `NamespacePanel` only calls
    `listPins()`; the demo `App.tsx` still separately calls
    `namespace.retrieve(name)` for each pin to prime the scope. If a
    later consumer mounts `WorkstationShell` without that warm-up
    and the namespace backend is lazy, BlockRef renders inside the
    namespace panel could throw "entity not found in any parent
    scope". Worth either documenting the warm-up requirement on
    `WorkstationShell` or doing it inside `NamespacePanel` if it
    turns out to be a common trap.
12. **Demo App still uses pre-retrieve loop.** Keeping it for now —
    a future step can move the warmup into `NamespacePanel` or a
    helper on `FxtrReadNamespace`. Carries the StrictMode /
    scope-cleanup caveats from step 3.
13. **Biome noise around `useMemo` deps on functions-of-`entity`.**
    The `useMemo` in `useEntityHoverCard` had to list `entity` (not
    just `entity.idString`) because biome's
    `useExhaustiveDependencies` can't see through the method call.
    Benign, but be aware the memo now re-computes on every new
    FxtrID reference — trivially cheap since the callback just
    builds a small object.
14. **Nested-preview test target.** `DemoSection` now ships a
    `HoverPreview` handler that embeds `<Render as={BlockRef}>` for
    the section's first item, so hovering a namespace row's section
    BlockRef → popup with the section summary + a nested BlockRef
    → hovering that inner BlockRef opens the note's preview popup
    on top. This exercises the nested-hover-keep-open path and
    close-on-navigate.
