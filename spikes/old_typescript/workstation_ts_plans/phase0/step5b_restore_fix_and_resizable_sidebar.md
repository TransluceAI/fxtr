# Step 5b — Layout-restore fix + resizable sidebar

Follow-up to step 5. Two bundled changes:

1. Fix a bug where restoring a persisted dockview layout breaks the
   namespace panel (and would break entity panels too) because class
   instances don't survive `toJSON` / `fromJSON`.
2. Port thingwatcher's `ResizableSidebar` so the open-panels rail has
   a draggable width.

## Background

### The restore bug

`WorkstationShell.onReady` restores layout with `api.fromJSON(persisted.dockview)`.
Dockview serializes panel params via `JSON.stringify`, which strips
all class methods. On restore, params become plain data objects.

Observed: `NamespacePanel` calls `namespace.listPins()` on the
restored namespace and crashes because `listPins` is missing. The
same problem is latent for entity panels — `entity.idString()`,
`entity.equals(...)`, and the full FxtrID API are gone after a
round-trip.

### The scope-bridging problem

Even if we could hydrate the FxtrID on the client, the scope doesn't
know about a restored entity unless it was reached through the
pin→retrieve warm-up or descended from an already-known entity.
`useFxtrLoad(entityRef)` on a cold restored entity would throw
`"Node <id> was not found in any parent scope!"`.

The fingerprinting report has the same problem with URL-restored
entities and solves it with a gating pattern (`ReportShell.tsx`):

- Take a `syncUrl.tryRetrieveById: (id) => Promise<FxtrID | null>`
  callback from the app.
- Collect every referenced FxtrID on restore, `await
  Promise.all(allIds.map(tryRetrieveById))` so the bridger can attach
  each entity's arena into the scope.
- Gate rendering on a `urlReady` flag flipped when bridging settles.

`HttpFxtrReadNamespace.tryRetrieveById` (in `fxtr-http-client`) is the
canonical implementation — it uses a deep-fetchable to pull the
entity's raw data via `/data/` and calls
`scope[FXTR_SCOPE_INTERNALS].unsafeAttachFromExternalArena` to bridge
it into the scope.

## Goal

After step 5b:

- Dockview panel params contain only JSON-safe primitives. A fresh
  reload of the dev demo successfully restores any previously-open
  panels: the namespace panel works, entity panels load their entities
  from the server into the scope on demand, and the sidebar renders
  correctly against the restored state.
- The open-panels sidebar has a draggable vertical edge to resize its
  width between a floor and a ceiling, with the width persisted in
  `localStorage` alongside `sidebarCollapsed`.

## Design

### Panel params — move class instances out

Current params:

```
NamespacePanelParams  = { namespace: FxtrReadNamespace }
EntityPanelParams     = { entity:    FxtrID             }
```

New params:

```
NamespacePanelParams  = {}                     // namespace flows via React context
EntityPanelParams     = { entityIdString: string }
```

Pieces:

- **`NamespaceContext`** (new, `src/workstation/NamespaceContext.tsx`):
  provides the `FxtrReadNamespace` to descendants. `WorkstationShell`
  wraps its tree in the provider; `NamespacePanel` reads the namespace
  with `useNamespace()`.
  - Keep `NamespacePanelParams` exported as `{}` (or a branded empty
    object) for the dockview type parameter.
- **`EntityPanelWrapper`**: params become `{ entityIdString: string }`.
  It reconstructs the FxtrID with `useMemo(() => FxtrID.fromIdString(entityIdString), [entityIdString])`.
  Its `<Render key=...>` keys off `entityIdString` (already effectively
  doing this — just the source changes).
- **Call sites** updated in `WorkstationShell`: `addEntityPanel`,
  `replaceEntityPanel`, the default-layout `initialPanels` loop, and
  the drag-drop handler in `DockviewLayout.onDidDrop` all write
  `entityIdString: target.idString()`.
- **`usePanelLayout.buildLayout`** lifts `entityIdString` from params
  and reconstructs the FxtrID once for the `SidebarPanel.entity` field
  (so `OpenPanelsSidebar`'s `<Render entity=...>` still works).
- Bump **`LAYOUT_STORAGE_VERSION = 2`** so any dev payloads persisted
  with the old schema are ignored on first boot.

### Scope-bridging on restore

`WorkstationShellProps` gains a required (when layout persistence is
enabled — which it always is right now) bridger:

```
tryRetrieveById: (id: FxtrID) => Promise<FxtrID | null>
```

The demo passes `(id) => ns.tryRetrieveById(id)`. If we later host a
non-HTTP namespace without a bridger, we'll need to revisit — options
noted in "Future" below.

Restore flow in `onReady`:

1. If no persisted layout → build the default layout, done.
2. If a persisted layout exists → `api.fromJSON(persisted.dockview)`.
   - Collect every `entityIdString` from the restored panels whose
     `component === PANEL_COMPONENT_ENTITY`.
   - `await Promise.all(ids.map(id => tryRetrieveById(FxtrID.fromIdString(id))))`.
   - For any id that came back `null` (entity gone from server),
     remove that panel from dockview — don't leave a panel that will
     crash on first render.
   - Bump the `entity-<n>` counter past the largest surviving id.

The shell also needs a `restoreReady` state flag that is:
- `true` immediately if there's no persisted layout to restore,
- flipped to `true` after bridging completes (success or partial).

While `restoreReady` is `false`, the shell renders a simple
placeholder (e.g. "Restoring workstation…") *instead of* the dockview
container + sidebar. Reason: we don't want `EntityPanelWrapper`s to
mount against a not-yet-bridged scope and hit the "not found" error.
Dockview's `onReady` fires once; we can use `setTimeout`/microtask
sequencing or simply: start bridging in `onReady`, and don't set `api`
state (which the sidebar/dockview container branch keys off) until
bridging settles. But `DockviewReact` is already mounted and has
probably already started rendering the restored panels by the time
`onReady` fires — so "don't set api state" isn't enough.

Cleaner approach: **defer `fromJSON` until the bridger has run**. That
is, in `onReady`:

1. Collect entity ids *from the persisted-layout JSON itself* (not
   from the live dockview). The JSON has panels + their params.
2. Await `tryRetrieveById` for each.
3. Filter out stale ids, mutate the persisted JSON to drop their
   panels (or rely on dockview's own "missing panel" handling — TBD in
   implementation), then call `api.fromJSON(...)`.
4. Ensure the namespace panel, bump the counter, set `api` state.

That way the panels are never mounted against a scope that doesn't
know about them.

Pseudocode:

```
onReady(api):
  const persisted = loadPersistedLayout()
  if (!persisted):
    ensureNamespacePanel(api)
    addInitialPanels(api)
    setApi(api); return

  const restoredEntityIds = collectEntityIdsFromJson(persisted.dockview)
  const results = await Promise.all(restoredEntityIds.map(id =>
    tryRetrieveById(FxtrID.fromIdString(id)).catch(() => null)
  ))
  const goodIds = new Set(restoredEntityIds.filter((_, i) => results[i] !== null))
  const cleaned = dropStalePanels(persisted.dockview, goodIds)

  try { api.fromJSON(cleaned) }
  catch { localStorage.removeItem(...); build default layout }

  ensureNamespacePanel(api)
  panelCounterRef.current = maxEntityCounter(api)
  setApi(api)
```

`dropStalePanels` walks the `SerializedDockview` JSON and removes any
panel whose `component === "entity"` and whose
`params.entityIdString` is not in `goodIds`. Groups that become empty
also need cleanup (dockview may handle this itself — verify in
implementation).

### ResizableSidebar

Near-direct port from `spikes/thingwatcher/thingwatcher/frontend/components/Sidebar/ResizableSidebar.tsx`.

- New: `fxtr-workstation-core/src/sidebar/ResizableSidebar.tsx`.
  - Same `mousedown/mousemove/mouseup` + min/max clamp logic.
  - Add an `onWidthChange?: (w: number) => void` callback so the shell
    can persist the width into its layout payload.
  - Accept a `width` prop (controlled) so the shell is the source of
    truth for persistence. (Thingwatcher's version is uncontrolled —
    we need controlled for persistence.)
- New: `fxtr-workstation-theme/styles/resizable-sidebar.css`.
  - `.fw-resizable-sidebar` (container: `position: relative; height: 100%; flex-shrink: 0`).
  - `.fw-resizable-sidebar__handle` (4px wide, right-edge, `cursor: col-resize`).
  - `.fw-resizable-sidebar__handle:hover { background: var(--accent); }`.
  - Add to `all.css` barrel.

Integration in `WorkstationShell`:

- Replace the direct `<OpenPanelsSidebar>` render with a branch:
  - If `sidebarCollapsed`: render `<OpenPanelsSidebar>` as today (it
    already renders its own 32px collapsed shell).
  - Otherwise: wrap in `<ResizableSidebar width={...} onWidthChange={...}>`
    with min 180, max 480, default 240 (matches current CSS).
- Width is tracked as a third piece of persisted state. Add
  `sidebarWidth: number` to `PersistedWorkstationLayout` and wire it
  through the same save/restore paths as `sidebarCollapsed`.

One subtlety: `OpenPanelsSidebar`'s open-state CSS currently has a
fixed `width: 240px` in the `.fw-sidebar--open` rule (assumed — we'll
verify). We need to remove the fixed width so `ResizableSidebar`'s
inline `width` style controls layout. The collapsed branch keeps its
fixed 32px.

## Implementation steps

Each checkbox is a discrete commit-sized unit. Do them in order.

- [x] 1. **Add `NamespaceContext`**. New file
       `src/workstation/NamespaceContext.tsx` with
       `NamespaceContextProvider` + `useNamespace()`.
- [x] 2. **Update `NamespacePanel` to use the context**. Drop the
       `namespace` param; read it from `useNamespace()`. Update
       `NamespacePanelParams` to `{}`.
- [x] 3. **Swap `EntityPanelParams`** to `{ entityIdString: string }`.
       Update `EntityPanelWrapper` to hydrate with
       `FxtrID.fromIdString` (memoized).
- [x] 4. **Update all producers** of panel params in `WorkstationShell`
       (`addEntityPanel`, `replaceEntityPanel`, `initialPanels` loop,
       namespace `ensureNamespacePanel`) and in `DockviewLayout.onDidDrop`.
       (Drop handler already passes a live FxtrID via `navigate` — no
       change needed there. All other sites write `entityIdString`.)
- [x] 5. **Update `usePanelLayout.buildLayout`** to read
       `entityIdString` and reconstruct the FxtrID (once per rebuild).
- [x] 6. **Add `tryRetrieveById` prop** to `WorkstationShellProps`
       (required). Thread it into `onReady`.
- [x] 7. **Rework restore flow** in `onReady`:
       - Collect entity ids from the persisted JSON.
       - Await `tryRetrieveById` for each, best-effort (null / thrown
         results are logged but not fatal).
       - Call `api.fromJSON(persisted.dockview)` straight — no layout
         mutation. Entities that didn't bridge mount as panels whose
         `scope.isTracked(entity)` is false; `EntityPanelWrapper`
         surfaces a friendly "Entity not available" body (see step
         7b).
       - (Simplification vs. the original plan: no `restoreReady`
         placeholder, no grid-tree pruning. Dockview starts empty,
         `setApi` only fires after the bridging + `fromJSON` settles,
         so drags during the restore window are no-ops.
         `apiRef.current` is set at the same time.)
- [x] 7b. **Per-panel error surface in `EntityPanelWrapper`**:
       - New `fallbacks/PanelErrorBoundary.tsx` — a React error
         boundary that renders a themed "Failed to render panel" body
         instead of bubbling rendering errors up to the whole shell.
       - In `EntityPanelWrapper`, call `useFxtrScope()` and, inside a
         `useMemo` keyed on entity:
         - Call `scope.lift(entity)` — walks parent scopes and
           bridges the entity's arena into the current scope if
           found (idempotent; safe to call render-time). This is
           necessary because `isTracked` only checks the current
           scope's main arena, while entities loaded through pin
           retrieval often live in a parent scope.
         - Read `scope.isTracked(entity)` for the branch decision.
       - Branch on the tracked result:
         - If tracked → wrap `<Suspense><Render .../></Suspense>` in
           `<PanelErrorBoundary>`.
         - If not tracked → render `EntityNotTrackedBody` directly.
       - This both replaces the need for stale-panel grid pruning at
         restore time and gives every entity panel a crash-safe
         boundary for unrelated render errors.
- [x] 8. **Bump `LAYOUT_STORAGE_VERSION` to 2**.
- [x] 9. **Add `sidebarWidth` to persisted layout**. Default 240.
       Add to `PersistedWorkstationLayout`; load at mount; save in
       the existing debounced save path; react to width changes.
- [x] 10. **Port `ResizableSidebar`** (controlled `width` +
        `onWidthChange`) and its CSS. Wire into `all.css` barrel.
- [x] 11. **Integrate in `WorkstationShell`**: when not collapsed,
        wrap `<OpenPanelsSidebar>` in `<ResizableSidebar>`; remove
        the fixed-width rule from `.fw-sidebar--open` so the inline
        width is authoritative.
- [x] 12. **Pass `tryRetrieveById` in the dev demo**
        (`example-dev-workstation/src/client/App.tsx`):
        `<WorkstationShell namespace={ns} tryRetrieveById={(id) => ns.tryRetrieveById(id)} />`.
- [ ] 13. **Manual test**: open dev demo, open some entity panels,
        reload, confirm they restore cleanly. Test with an entity id
        that isn't on the server (edit localStorage by hand) and
        confirm it's dropped on restore instead of crashing.
        Test resize handle + persistence.
- [x] 14. **Update `CODE_OVERVIEW.md`** — note the params-schema
        shift, the `NamespaceContext`, the `tryRetrieveById` prop on
        `WorkstationShell`, the restore bridging step, and the
        `ResizableSidebar` component + style file.

## Future

- **Non-HTTP namespaces**: `tryRetrieveById` is currently required
  whenever layout persistence is on. If we later want to host a
  namespace without a deep-fetchable bridger, options:
  - Disable layout persistence for that consumer (drop the
    `localStorage` round-trip entirely).
  - Require the consumer to pre-seed the scope with every entity that
    *could* be in a restored panel (today that means: don't close
    panels, don't persist layout — effectively the same).
  - Push restored panels through `navigate({ kind: "open-new" })`
    rather than `fromJSON`, so the normal retrieval path runs. This
    loses the grid/group structure of the saved layout.
- **Breadcrumb-history restore**: when we add per-panel breadcrumb
  history (alluded to in `WorkstationShell`'s panel-id policy), the
  restore bridger will also need to pull in every entity in every
  panel's history, not just the current entity.
