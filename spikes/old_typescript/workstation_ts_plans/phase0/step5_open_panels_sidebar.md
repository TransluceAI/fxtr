# Step 5 — Naming refactor + Open-panels sidebar + Layout persistence

Step 5 lands three things, in this order:

1. **Naming refactor** — split the interactive wrapper out of the
   dispatch registry into a standalone `<EntityLink>` component;
   rename the dispatch keys off "Ref" (which collides with fxtr's own
   `EntityRef` concept); matching fallback / prop-type renames.
2. **Open-panels sidebar** — a left-column list of currently-open
   dockview panels with per-row tooltips (shared-timeout style),
   click-to-focus, X-to-close, and a collapse/expand toggle.
3. **Layout persistence** — dockview state + sidebar-collapsed state
   serialized to localStorage, restored on mount with a sensible
   fallback if anything looks wrong.

## Why the refactor is in the same step

The sidebar wants to render panels' entities using the `BlockLink`
renderer for display but **not** pick up the click/drag/hover/context
interactions that the rest of the workstation attaches to refs. Under
the step-4 design, interactions live inside the registry via
`EntityRefRoot` auto-wrap, so `<Render as={BlockRef}>` is unavoidably
interactive — there's no pure-display path.

The clean fix is to pull interactions out of the dispatch layer:

- Registry goes back to being **pure display**. `<Render as={BlockLink}>`
  produces content only.
- A new `<EntityLink>` component is the public composition of display
  + interactions. It's what ordinary consumers reach for.
- The sidebar uses `<Render>` directly, so its rows are non-interactive
  at the ref level; the row itself owns click (focus) and X (close).

Naming collision: we currently call both the dispatch keys and the
concept "Ref", which also overloads `EntityRef` in fxtr core. Dropping
"Ref" in the workstation's public API makes the code easier to read.

## Decisions (resolved in pre-plan discussion)

1. **Component name:** `<EntityLink entity as="block" | "inline" linkText?>`. Single component, `as` chooses the variant. Dispatch keys renamed `BlockLink` / `InlineLink`.
2. **`FullPanel` and `HoverPreview` keys unchanged.** No "Ref" problem there.
3. **`EntityRefRoot` is not re-exported under a new name.** Its composition logic moves inside `EntityLink.tsx` as an internal helper. Exotic consumers who want interactions without dispatched content compose the four hooks (`useDraggableEntity`, `useEntityClick`, `useEntityHoverCard`, `useEntityContextMenu`) directly — those stay exported.
4. **`DraggableEntityHandle` stays.** The name is idiomatic; overload with thingwatcher's "handle" terminology doesn't cross codebases.
5. **Registry drops auto-wrap.** `createWorkstationDispatchHandler` goes back to appending `unknownLoadCodec` fallbacks only.
6. **Sidebar row click = focus panel.** No breadcrumb emission. Focusing an already-open panel isn't navigation.
7. **Grouping.** Sidebar entries are grouped by dockview group (matches thingwatcher). Each group shows a header (`Group 1`, `Group 2`, …) above its rows.
8. **Close-button policy.** The X is suppressed for the namespace panel (id `namespace-main`). Clicking it would just trigger the auto-reopen path.
9. **Non-entity panels** show plain title text in the sidebar (no BlockLink dispatch).
10. **SidebarTooltip** uses base-ui's `Tooltip` component (not `PreviewCard`): `Tooltip.Provider` gives shared-delay grouping for free, and `disableHoverablePopup={true}` on `Tooltip.Root` closes the popup on trigger-leave. No custom shared-state context of our own.
11. **Drag-from-sidebar is out of scope.** Click-focus / X-close only. Flag as follow-up.
12. **Drag-to-resize the sidebar is out of scope.** Fixed width (240px) when open. Flag as follow-up.
13. **Collapse/expand toggle is in scope.** Sidebar collapses to ~32px (just enough for the toggle button) and expands back. State persisted with layout.
14. **Layout persistence:** `onDidLayoutChange` → 200ms debounced write to localStorage. Restore on mount; on parse failure, fall back to the default layout (namespace panel only). Namespace panel re-added if missing from a restored layout. Panel-id counter bumped past max `entity-<n>` seen.
15. **No new shell chrome.** `WorkstationShell` still contains only sidebar + dockview; the consumer provides their own header.

## Naming refactor — files touched

- `registry.tsx` → remove `maybeWrapRefEntry` + `withRefInteractions`; becomes the pre-step-4 shape again. Still named `registry.tsx`.
- `keys.ts` → rename `BlockRef` → `BlockLink`, `InlineRef` → `InlineLink`. Rename `RefProps` → `LinkProps`. `FullPanel`, `HoverPreview` unchanged.
- `interactions/EntityRefRoot.tsx` → deleted. Its composition logic folds into `EntityLink.tsx`.
- `EntityLink.tsx` (new, at `src/EntityLink.tsx`) → public component.
- `fallbacks/BlockRefFallback.tsx` → `fallbacks/BlockLinkFallback.tsx`.
- `fallbacks/InlineRefFallback.tsx` → `fallbacks/InlineLinkFallback.tsx`.
- `src/index.ts` → update exports.
- `components/JsonTree.tsx` → `<Render as={InlineRef}>` → `<EntityLink as="inline">`.
- Demo `src/client/handlers.tsx` → `<Render as={BlockRef}>` → `<EntityLink as="block">` (in `DemoSectionFullPanel` and `DemoSectionHoverPreview`).

### `EntityLink` shape

```tsx
export interface EntityLinkProps extends LinkProps {
  entity: FxtrID;
  as?: "inline" | "block";
}
```

Internally:
```tsx
function EntityLink({ entity, as = "block", ...linkProps }: EntityLinkProps) {
  const HoverWrap = useEntityHoverCard(entity, as);
  const ContextMenuWrap = useEntityContextMenu(entity);
  const { onClick } = useEntityClick(entity);
  const { draggable, onDragStart } = useDraggableEntity(entity);

  const key = as === "inline" ? InlineLink : BlockLink;
  const Tag = as === "inline" ? "span" : "div";

  return (
    <HoverWrap>
      <ContextMenuWrap>
        <Tag onClick onDragStart draggable title={TITLE} style={...}>
          <Render entity={entity} as={key} {...linkProps} />
        </Tag>
      </ContextMenuWrap>
    </HoverWrap>
  );
}
```

Sidebar rows and other non-interactive use sites call `<Render
entity={id} as={BlockLink} />` directly.

## Open-panels sidebar

### `sidebar/usePanelLayout.ts`

```ts
interface SidebarPanel {
  panelId: string;
  component: string;            // "entity" | "namespace" | ...
  title: string | undefined;    // dockview panel title
  entity?: FxtrID;              // present only for "entity" panels
  isActive: boolean;            // the active panel within its group
}

interface SidebarGroup {
  groupId: string;
  panels: SidebarPanel[];
}

function usePanelLayout(api: DockviewApi | null): SidebarGroup[];
```

Implementation mirrors thingwatcher's `usePanelLayout.ts`: subscribe
via `api.onDidAddPanel`, `onDidRemovePanel`, `onDidLayoutChange`,
`onDidActivePanelChange`; rebuild the group list on each event.
`entity` is pulled from `panel.params.entity` for `"entity"` panels.

### `sidebar/SidebarTooltip.tsx`

Ported straight from thingwatcher's `SidebarTooltip.tsx`. Uses base-ui
`Tooltip` (not `PreviewCard`); two primitives handle the behavior we
want with no custom context of our own:

- `Tooltip.Provider` — wrapped around the sidebar row list. Gives
  shared-delay grouping for free: the first hover waits the delay,
  subsequent hovers on other tooltips open instantly (so scanning a
  panel list feels snappy).
- `Tooltip.Root disableHoverablePopup={true}` — popup closes on
  trigger-leave; cursor moving into the popup itself doesn't keep it
  open. Sidebar tooltips aren't nested-hover entry points.

Popup content: `<Render entity={id} as={HoverPreview} />` inside a
Suspense boundary — same dispatch path as the normal hover preview,
just a different trigger wrapper.

Popup class: `fw-hover-popup` (reuse theme class; visuals are the same,
only the timing differs).

Module re-exports `Tooltip.Provider` as `SidebarTooltipProvider` for
clarity at the sidebar call site, but it's a trivial re-export — no
custom provider component.

### `sidebar/OpenPanelsSidebar.tsx`

```tsx
interface OpenPanelsSidebarProps {
  api: DockviewApi;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}
```

- When `collapsed === true`: render a narrow rail (`width: 32px`) with
  just the toggle button.
- When `collapsed === false`: render the full sidebar:
  - Top row: "Panels" header + toggle button (`<`).
  - Below: `usePanelLayout(api)` → map each `SidebarGroup` to a
    `<GroupSection>` (`Group N` header + rows).
  - Wrap the whole row list in `SidebarTooltipProvider`.
  - Each row:
    - Whole row element is a `<div role="button">` with `onClick` →
      `panel.api.setActive()`. Active-panel row has a highlight
      class.
    - Content: for `"entity"` panels, `<SidebarTooltip entity={id}><span className="fw-sidebar-row__label"><Render entity={id} as={BlockLink} /></span></SidebarTooltip>`; for others, `<span className="fw-sidebar-row__label">{panel.title ?? panel.component}</span>`.
    - Trailing: X button (suppressed when `panelId === "namespace-main"`).

No breadcrumb emission; the sidebar is nav-aid, not nav-event.

### CSS (new `fxtr-workstation-theme/styles/sidebar.css`)

```
.fw-sidebar {
  display: flex;
  flex-direction: column;
  background: var(--surface-sunken);
  border-right: 1px solid var(--border-subtle);
  overflow: hidden;
  font-family: var(--font-prose);
  font-size: var(--text-body-sm);
}
.fw-sidebar--open { width: 240px; flex: 0 0 240px; }
.fw-sidebar--collapsed { width: 32px; flex: 0 0 32px; }

.fw-sidebar__header { /* "Panels" label + collapse button */ }
.fw-sidebar__group-header { /* "Group N" muted caps */ }
.fw-sidebar__row { /* flex row, hover bg, cursor pointer */ }
.fw-sidebar__row--active { background: var(--accent-subtle); }
.fw-sidebar__row-label { /* flex: 1; truncation */ }
.fw-sidebar__close { /* small icon button */ }
.fw-sidebar__toggle { /* shared with the collapsed-rail toggle */ }
```

Imported from `all.css`.

## Layout persistence

### Serialized shape

```ts
interface PersistedWorkstationLayout {
  version: 1;
  dockview: SerializedDockview;   // from api.toJSON()
  sidebarCollapsed: boolean;
}
```

Stored at `localStorage["fxtr-workstation-layout"]`. Versioned; a
mismatch triggers the default-layout fallback.

### Save path

- `api.onDidLayoutChange` fires on every resize/move/add/remove.
- Handler captures the next write, debounced 200ms via a ref-held
  timeout.
- Also saves on `sidebarCollapsed` change (not debounced; it's
  single-event).

### Restore path

On dockview ready:
1. Read `localStorage["fxtr-workstation-layout"]`. If absent or
   JSON-unparseable, fall through to default layout.
2. If `parsed.version !== 1`, log and fall through to default.
3. Try `api.fromJSON(parsed.dockview)`. If it throws, log and fall
   through to default.
4. Verify the namespace panel exists; if missing, add it.
5. Scan all entity panel ids for the `entity-<n>` pattern and bump
   `panelCounterRef.current` past the max `n` found.
6. Apply `parsed.sidebarCollapsed` to shell state.

Default layout (unchanged from step 4): namespace panel only; no
entity panels.

### Failure modes to handle

- `api.fromJSON` throwing on a stale-schema layout → swallow, reset
  localStorage entry, build default.
- Restored layout missing `namespace-main` → add it.
- Restored layout containing an entity panel whose entity id can't be
  parsed or loaded → the existing `<Render>` error path takes over
  (dispatch renders the registered `FullPanelFallback` for
  contentless/missing ids; nothing crashes the shell).
- Counter collision during runtime if a restored id matches a newly-
  minted one → avoided by the counter bump step.

## `WorkstationShell` — structural changes

- Outer layout becomes `display: flex; flex-direction: row`.
- `<OpenPanelsSidebar api={api} collapsed={sidebarCollapsed} onToggleCollapsed={...} />` is the first child.
- `<DockviewLayout ... />` is the second child, `flex: 1 1 auto`.
- Sidebar is mounted *after* `apiRef.current` is populated (on `onReady`); rendered via a state variable that flips true once ready.
- New state: `sidebarCollapsed: boolean` (initial `false`, overwritten by restore).
- Layout persistence hooks wired inside `onReady` as described above.
- Provides `WorkstationContext`, `HoverCloseContext` as before.

## Demo updates

- `DemoSectionFullPanel` item list: `<Render entity={...} as={BlockRef} />` → `<EntityLink entity={...} as="block" />`.
- `DemoSectionHoverPreview` nested ref: same swap.
- No new demo behaviors; step-5 wiring is "wrap the shell and see the sidebar."

## Implementation order

Roughly dependency order:

- [x] Rename registry: drop `maybeWrapRefEntry`, restore to pre-step-4
      shape; keep appending `unknownLoadCodec` fallbacks. File renamed
      from `registry.tsx` back to `registry.ts` (no more JSX).
- [x] Rename dispatch keys: `BlockRef` → `BlockLink`,
      `InlineRef` → `InlineLink`. Rename `RefProps` → `LinkProps`.
- [x] Rename fallback files + their component names:
      `BlockRefFallback` → `BlockLinkFallback`,
      `InlineRefFallback` → `InlineLinkFallback`.
- [x] Delete `interactions/EntityRefRoot.tsx`.
- [x] Create `src/EntityLink.tsx` with the public component; fold
      the old root composition logic inside.
- [x] Update `src/index.ts` exports (add `EntityLink`, drop
      `EntityRefRoot`, rename the key exports).
- [x] Update `src/components/JsonTree.tsx` to use `<EntityLink>`.
- [x] Update `panels/NamespacePanel.tsx` to use `<EntityLink>`
      (previously relied on registry auto-wrap; without it the
      namespace rows would lose their interactions).
- [x] Update demo `src/client/handlers.tsx` (`DemoSectionFullPanel`
      and `DemoSectionHoverPreview`) to use `<EntityLink>`. Renamed
      `DemoNoteBlockRef` → `DemoNoteBlockLink`; updated docstrings +
      server note text; registry entry uses `BlockLink`.
- [x] `pnpm check` + `pnpm lint` clean after refactor.
- [x] Theme: `styles/sidebar.css` with `.fw-sidebar*` classes. Import
      in `all.css`, add subpath export in `package.json`.
- [x] `sidebar/usePanelLayout.ts` — tracks dockview api events.
- [x] `sidebar/SidebarTooltip.tsx` — port of thingwatcher's tooltip
      using base-ui `Tooltip.Root disableHoverablePopup={true}`; re-
      exports `Tooltip.Provider` as `SidebarTooltipProvider`.
- [x] `sidebar/OpenPanelsSidebar.tsx` — the sidebar component itself.
- [x] Update `WorkstationShell.tsx`:
      - Flex-row layout wrapping sidebar + dockview.
      - `sidebarCollapsed` state + toggle callback (read from
        persisted payload at mount).
      - Layout persistence: debounced 200ms onDidLayoutChange write,
        `api.fromJSON` restore on ready with fallback to default,
        counter bump via `maxEntityCounter`, namespace-panel verify,
        sidebarCollapsed sync.
- [x] Update `src/index.ts` with new sidebar exports
      (`OpenPanelsSidebar`, `SidebarTooltip`,
      `SidebarTooltipProvider`, `usePanelLayout`, `SidebarPanel`,
      `SidebarGroup`).
- [x] `pnpm check` + `pnpm lint` clean.
- [x] Smoke-tested via curl: `/pins` returns all three pins; HTML +
      `/src/client/main.tsx` return 200. **Browser visual smoke
      (sidebar visible, rows per group, clicks focus, X closes except
      for namespace, collapse/expand, reload preserves layout +
      collapsed state, sidebar tooltips scan with shared delay)
      pending user.**
- [x] Update `projects/fxtr_workstation/ts/CODE_OVERVIEW.md`.
- [x] Update `projects/fxtr_workstation/ts/plans/phase0/overview.md`
      checkbox for step 5.

## Out of scope (still / newly)

- Drag-from-sidebar to open duplicate panels (follow-up).
- Drag-to-resize the sidebar (follow-up).
- Shell-provided chrome / title bar (consumer owns their header).
- Per-panel breadcrumb-history storage (post-phase-0).
- Visible breadcrumb UI (phase 1).
- Dynamic rendering (phase 1).

## Follow-ups / risks surfaced during implementation

1. **`SidebarTooltip` trigger element.** base-ui `Tooltip.Trigger`
   defaults to `<a>`. We `render={<span style={{display:"block",
   minWidth:0}} />}` so the tooltip anchors off a real box that fills
   the row cell without injecting an `<a>` around an arbitrary DOM
   sub-tree (same bounding-box rationale as `useEntityHoverCard` in
   step 4).
2. **Row click vs. inner click.** The sidebar row is a
   `<div role="button">` wrapping the label and close button. Clicks
   on the close button call `stopPropagation` so they don't also
   focus. The inner label is just a `<Render as={BlockLink}>` (no
   interactions), so there's no click collision there.
3. **`maxEntityCounter` after restore.** We bump the counter past the
   largest `entity-<n>` found in the restored layout so new panels
   don't collide. If someone manually hand-edits localStorage with
   non-numeric ids the regex just skips them; fine. If the counter
   happens to match a restored id because of some future divergent
   id scheme, `api.addPanel` will throw — not a concern today.
4. **Layout persistence flush on unmount.** The debounced save has
   its last write flushed synchronously in the cleanup path if a
   timer is pending, so a very rapid unmount after a layout change
   doesn't lose the final state.
5. **`sidebarCollapsed` write on entity-change effect.** The
   collapsed-state effect calls `savePersistedLayout` immediately
   (no debounce). It does so on every toggle, which triggers a new
   full `api.toJSON()` call; acceptable here since toggles are rare
   user actions, not layout-drag storms.
6. **SidebarTooltip inside a role=button div.** `<Tooltip.Trigger>`
   injects its own focus/hover handlers. So far no conflict with the
   row's `onClick`/`onKeyDown`; flag for user smoke testing in case
   keyboard navigation behaves oddly.
7. **`fromJSON` failure doesn't surface to the user.** We
   `console.warn`, clear the localStorage key, and rebuild the
   default. If this becomes a silent footgun (e.g. someone's saved
   layout gets zapped by a minor dockview version bump), consider a
   user-visible toast or a one-time message.
8. **`useSyncExternalStore` snapshot stability.** `usePanelLayout`
   mutates its cached array in place during `subscribe`'s
   rebuild-and-notify and returns the same ref from `getSnapshot`
   until a new notification. React 18+ tolerates in-place mutation
   as long as the reference changes on change, which it does; but
   worth remembering if we later want immutable guarantees for the
   sidebar's own memoization.
9. **Drag-from-sidebar + drag-to-resize** were deferred (per the
   decisions section). Both are easy to add later without touching
   the rest of the shell.
10. **`EntityLink` is slightly heavier than the old auto-wrap.**
    Each ref instance now sets up four hooks' worth of state, which
    previously happened inside `EntityRefRoot`. Same runtime cost;
    worth noting that "rendering many refs" does incur that cost
    and the sidebar intentionally skips it by using `<Render>`
    directly.
