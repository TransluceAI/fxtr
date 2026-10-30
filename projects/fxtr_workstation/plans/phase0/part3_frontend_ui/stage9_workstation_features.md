# Stage 9 — Workstation features in panels

Beyond visual parity (stage 8): add the workstation-only
affordances that turn the shell into something you can actually
use to inspect what's happening on the server. Three big strands:

- **Debug surfaces.** Workstation-level debug panel showing the
  four resource lists and an inflatable per-session view, plus a
  separate per-session debug panel kind that subscribes to one
  session and surfaces its low-level state. A force-terminate
  button gives the user an escape hatch when a session is wedged.
- **Metadata view as a new panel mode.** A new way of looking at
  a ref: show its workstation metadata (entity / service) +
  raw loaded data (for entities) + `$fxtr.ctx` / `$fxtr.type`
  results — alongside the existing dispatched view. Reachable
  via right-click on any `RefLink` or `DebugRefChip`.
- **Demo service renderers.** Hand-written FullPanel components
  for the demo workstation's services (echo, counter, a new
  callback-stream service) so the workstation demo finally
  exercises the service half of the shell end-to-end.

Plus a connection-status indicator in the shell header so
WebSocket drops stop being silent.

## Status

Chunk progress:

- [x] Chunk 1 — `StubRef` → `PanelViewEntry` refactor (required `display` field)
- [x] Chunk 2 — Metadata view component + `PanelWrapper` render branch
- [x] Chunk 3 — Right-click menu items + breadcrumb/sidebar/history-entry rendering for metadata entries
- [x] Chunk 4 — Connection status indicator in shell header
- [x] Chunk 5 — `DebugRefChip` (RefLink-like, no dispatch on render)
- [x] Chunk 6 — Workstation debug panel (new dockview panel kind) + sidebar "+ Debug" button
- [x] Chunk 7 — Per-session debug panel (new dockview panel kind) + Force-terminate
- [x] Chunk 8 — Demo service renderers (echo, counter, callback-stream)
- [x] Update `CODE_OVERVIEW.md` + `LOCAL_CODE_OVERVIEW.md`

Scratch design notes live in
[`stage9_workstation_features_scratch.md`](./stage9_workstation_features_scratch.md).

## Design decisions

These are the load-bearing choices. Open subdecisions live at the
bottom of this file.

- **`PanelViewEntry` replaces `StubRef` as the chain / drag /
  breadcrumb / follow-link entry type.** Every existing use of
  `StubRef` was structurally "a thing you can open in a panel".
  Renaming makes that intent explicit and lets us extend it with
  a `display` mode that propagates cleanly through drag, history,
  breadcrumbs, and history-root preservation.
- **`display: "default" | "metadata"` is required.** Forces
  every site to be explicit. Wire overhead is negligible — these
  records are tiny. Equality is plain field comparison.
- **`sameRef` compares `display`.** A metadata-flavored entry for
  ref X is a distinct chain entry from the default-flavored one
  for the same X. "Open metadata here" on the currently-active
  ref appends a new entry past the active position.
- **Two new panel kinds, both stateless (no own session):**
  `workstation_debug` and `session_debug`. Like `roots`, they
  carry only the discriminator (plus `sessionId` for the latter)
  in their dockview params and skip the session machinery
  entirely.
- **Debug-panel short-ID chips render fixed content, not via
  dispatch.** A new `DebugRefChip` component wires the same
  interaction hooks as `RefLink` (drag, click, right-click) but
  renders a plain monospace chip. Click → open default view of
  the ref in a new panel. Right-click offers both "Open in new
  panel" and "Open metadata in new panel". No hover preview
  (that would go through dispatch and track the ref in the
  dispatcher session — circular for the debug panels).
- **Metadata view = hardcoded component, not a dispatched
  renderer.** `PanelWrapper` branches on
  `activeEntry.display === "metadata"` before invoking
  `RenderDispatch`. Two variants: entity + service. Lives in
  `@fxtr-ui/workstation` so it's available regardless of the
  consumer's dispatch registry.
- **Force-terminate is a no-confirmation button.** Users
  reaching the per-session debug panel are already debugging;
  the button calls
  `connector.attachSession(id).terminate()` and immediately
  closes the panel (the panel auto-renders an "unavailable"
  state when the target session's lifecycle flips to `closed`).
- **Connection status indicator in the existing top header.** The
  header was previously conditional on `debugUiUrl`; it becomes
  always-rendered, displaying a colored badge + status text +
  workstation URL + optional debug-UI link.
- **Demo service renderers live in the demo, not in
  `@fxtr-ui/workstation`.** The workstation package ships
  fallback `Service*Fallback` renderers (already present); the
  demo's `dispatch_registry.tsx` is where we wire concrete
  renderers for the demo's services.

## Chunks

### Chunk 1 — `StubRef` → `PanelViewEntry` refactor

Foundational. Mechanical renames + the new `display` field +
equality updates. No new UI yet.

Files touched (in `frontend_lib/packages/fxtr-ui-workstation/src/`
unless noted):

- `types.ts` — rename `StubRef` → `PanelViewEntry`; add required
  `display: "default" | "metadata"` field; rename
  `stubRefOf(stub)` → `panelViewEntryOf(stub)` returning
  `display: "default"`.
- `breadcrumb/types.ts` / `breadcrumb/history.ts` /
  `breadcrumb/flatten.ts` / `breadcrumb/BreadcrumbBar.tsx` /
  `breadcrumb/BreadcrumbEntry.tsx` — pick up the rename;
  `sameRef` in `history.ts` compares `display` too.
- `panel/PanelContext.tsx` / `panel/PanelWrapper.tsx` — chain
  entries are `PanelViewEntry`; active entry's `.display` read
  but rendering branch comes in chunk 2 (this chunk just sets
  `display: "default"` and renders the existing way).
- `reflink/drag.ts` / `reflink/useRefContextMenu.tsx` /
  `reflink/useRefClick.ts` / `reflink/useDraggableRef.ts` /
  `reflink/useDraggableChainEntry.ts` /
  `reflink/useDraggableHistoryEntry.ts` /
  `reflink/RefLink.tsx` — rename; right-click menu still has
  the single "Open in new panel" item (extended in chunk 3).
- `shell/WorkstationApi.ts` / `shell/WorkstationShell.tsx` /
  `shell/DockviewLayout.tsx` — rename; `followLink.target` is
  `PanelViewEntry`. New entries built by the shell default to
  `display: "default"`.
- `sidebar/OpenPanelsSidebar.tsx` / `sidebar/SidebarTooltip.tsx`
  / `sidebar/usePanelLayout.ts` — rename.
- `index.ts` — re-export with new names.
- `panels/RootsPanel.tsx` — rename.
- Persisted layouts: `RefPanelChainState`'s shape stays the
  same as a JSON object — the chain entries gain `display`
  field on save. Old persisted layouts without `display` will
  *not* be auto-migrated; document this. Either bump
  `LAYOUT_STORAGE_VERSION` so old layouts get cleared on first
  load, or add a `display: "default"` default at load time.
  **Decision:** bump the version (cleaner and the only cost is
  a one-time layout reset on upgrade).

End-of-chunk verification:

- Type-check + biome pass.
- `pnpm dev` in `fxtr-ui-workstation-demo` — boots, default
  workstation behavior unchanged.
- Local layout: persisted v4 layout from before the bump
  clears cleanly; new v5 layout persists and restores.

### Chunk 2 — Metadata view component + render branch

Build the entity + service metadata renderers and the
`PanelWrapper` branch that activates them.

New files in
`frontend_lib/packages/fxtr-ui-workstation/src/metadata/`:

- `EntityMetadataView.tsx` — takes `stub: EntityRefStub`. Uses
  `useSessionRootStateAtom<MetadataViewState>` for stable
  per-panel state. State shape:
  ```
  { metadata?: OpResultStub<EntityMetadata>;
    load?:     OpResultStub<unknown> }
  ```
  On mount, allocates both via a function-form updater. Renders
  three sections:
  1. **Metadata** — fields from `EntityMetadata`. Ref-typed
     fields render as `<RefLink as="inline">`. String fields
     render as `<code>`-styled spans.
  2. **`$fxtr.ctx` / `$fxtr.type`** — pulled from the loaded
     value's top-level keys (since for entities these are *fields*
     in the loaded data, per design clarification). Each rendered
     via `<RefLink as="block">` if it's a ref, else as JSON.
  3. **Raw data** — collapsible JSON tree of the loaded value
     with `FxtrID` / `EntityRefStub` leaves rendered as
     `<RefLink as="inline">`. Other types render as plain JSON.
     The JSON tree may need its own small component; see open
     question on tree implementation.
- `ServiceMetadataView.tsx` — takes `stub: ServiceRefStub`. State:
  ```
  { metadata?: OpResultStub<ServiceMetadata>;
    ctxInvoke?: OpResultStub<unknown>;
    typeInvoke?: OpResultStub<unknown> }
  ```
  On mount, allocates metadata via `allocateGetServiceMetadata`
  and the two invokes via `allocateInvoke(stub, "$fxtr.ctx", [])`
  / `allocateInvoke(stub, "$fxtr.type", [])`. Renders the same
  three-section layout: metadata + ctx/type results + (no raw
  data — services don't have data; section omitted).
- `JsonRefTree.tsx` — small recursive tree component. Walks a
  JSON-or-stubs value; renders objects + arrays as
  collapsible nodes, primitives as styled spans, ref leaves
  (`EntityRefStub` / `ServiceRefStub` instances) as
  `<RefLink as="inline">`. Borrows shape from the debug UI's
  `JsonTree.tsx` but extended for ref rendering.
- `index.ts` — barrel for the metadata-related exports.

Modified:

- `panel/PanelWrapper.tsx` — in `PanelWrapperLive`, before the
  `RenderDispatch` call, branch on `activeEntry.display`. If
  `"metadata"`, render the entity or service variant; else
  current behavior. The metadata view sub-tree still lives
  inside `<BreadcrumbSourceProvider>` so RefLinks within it
  contribute to the trail.
- `index.ts` — export `EntityMetadataView` /
  `ServiceMetadataView` / `JsonRefTree`.

End-of-chunk verification:

- `pnpm dev` in `fxtr-ui-workstation-demo`. Manually open a
  metadata-mode entry by editing `localStorage`'s persisted
  layout (or via a temporary debug button) and verify both
  variants render. Production-feel right-click access lands in
  chunk 3.

### Chunk 3 — Right-click menu + metadata-aware breadcrumb / sidebar / history rendering

Wire metadata mode into the user-facing surfaces.

Modified:

- `reflink/useRefContextMenu.tsx` — expand the menu:
  1. "Open in new panel" (existing — `display: "default"`)
  2. "Open metadata in new panel" (new — `display: "metadata"`)
  3. **Inside a panel only:** "Open here"
     (`replace-in-source`, `display: "default"`)
  4. **Inside a panel only:** "Open metadata here"
     (`replace-in-source`, `display: "metadata"`)
  All four route through the existing
  `followLink` / `restoreIntoNewPanel` machinery; the only
  difference is the `display` field of the constructed
  `PanelViewEntry`.
- `breadcrumb/BreadcrumbEntry.tsx` — when entry has
  `display: "metadata"`, render a small "Metadata:" prefix
  badge before the dispatched `BlockLink`. Visual spec: pill
  with `var(--text-caption)` uppercase, matching the existing
  link-tag style in `handlers.tsx`.
- `breadcrumb/HistoryRootEntry.tsx` — same prefix treatment for
  metadata entries in flattened history.
- `sidebar/OpenPanelsSidebar.tsx::RefSidebarRow` — same prefix
  on the active stub's row label.
- `sidebar/SidebarTooltip.tsx` — for metadata entries, show a
  plain "Metadata: <shortId>" body rather than dispatching a
  `HoverPreview` (avoids tracking in dispatcher session).
- `reflink/RefLink.tsx` — for `<RefLink>` instances whose
  `stub` came from the chain and the entry has
  `display: "metadata"`, the inline / block rendering still
  goes through dispatch (the body is the dispatched component);
  this is fine because `RefLink` only renders content refs, not
  the active stub itself. The dispatcher-tracking concern only
  applies to *the active stub's renderer* (which is now the
  hardcoded metadata view, not dispatched).

End-of-chunk verification:

- `pnpm dev` in `fxtr-ui-workstation-demo`. Right-click a
  RefLink → all four menu items appear. Each lands in the
  expected mode. Breadcrumb shows metadata entries with the
  prefix. Drag-and-drop a metadata entry into a new tab —
  the drag carries `display: "metadata"` and the new panel
  opens in metadata mode.

### Chunk 4 — Connection status indicator

Always-render the header; add the colored badge.

Modified:

- `shell/WorkstationShell.tsx` — replace the conditional
  `DebugUiHeader` with an always-rendered `ShellHeader`
  component that takes `(connector, debugUiUrl?)`.
- New `shell/ShellHeader.tsx` — reads
  `connector.connectionStatus` via jotai, renders the badge +
  status text + workstation URL + optional debug-UI link.
  Visual style follows `fxtr-ui-demo/src/client/ConnectionStatusBar.tsx`
  with the existing `tokens.css` palette where applicable.
- `styles/` — add a new `shell-header.css` or fold into an
  existing file; barrel via `all.css`.

End-of-chunk verification:

- `pnpm dev` in `fxtr-ui-workstation-demo`. Header shows
  green-dot "open" + URL + "debug UI ↗" link. Killing the
  Python backend turns the dot orange ("reconnecting") then
  red ("closed") if reconnection fails.

### Chunk 5 — `DebugRefChip` component

New component used by the debug panels for inline short-ID
rendering. Lives in `@fxtr-ui/workstation` so the debug panels
in chunks 6/7 can use it.

New file `reflink/DebugRefChip.tsx`:

- Props: `{ ref: PanelViewEntry }`. Builds the concrete stub via
  the connector and wires the same four interaction hooks as
  `RefLink` (`useRefHoverCard` — actually skipped here;
  `useRefContextMenu`; `useRefClick`; `useDraggableRef`).
- Render body: a styled monospace span showing the short ID,
  with a small kind-aware prefix tag (`E:` / `S:`) so users
  can tell entity from service at a glance.
- Hover preview: omitted (would go through dispatch).
- Click behavior: standard `useRefClick` against a non-panel
  context (debug panels do not provide `PanelContext`, so the
  hook routes through `restoreIntoNewPanel`). Click ⇒ opens
  default view in new panel.
- Right-click: standard `useRefContextMenu`. Outside a panel
  context, only the new-panel items are shown (consistent
  with the existing menu's branching).

End-of-chunk verification:

- Renders cleanly in an isolated test page (small dev fixture
  in the demo, or a temporary section in the workstation
  debug panel during chunk 6 work).

### Chunk 6 — Workstation debug panel

New stateless dockview panel kind. Layout: four resource tables
(rpc_connections / services / entities / sessions) sourced from
`connector.list*({debug: true})`, with the session table rows
sprouting "open session debug" buttons.

New files in
`frontend_lib/packages/fxtr-ui-workstation/src/panels/`:

- `WorkstationDebugPanelParams.ts` — `{kind: "workstation_debug"}`
  + `WORKSTATION_DEBUG_PANEL_COMPONENT` const.
- `WorkstationDebugPanel.tsx` — the dockview content
  renderer. Reads the four `listX({debug:true})` reactive
  lists; renders four tables. Short IDs throughout rendered
  via `<DebugRefChip>`. Each sessions-table row has an
  "open debug" button calling
  `dockviewApi.addPanel({component: SESSION_DEBUG_PANEL_COMPONENT, ...})`.
  Doesn't provide `PanelContext` (so embedded chip clicks
  route through `restoreIntoNewPanel`).
- `tables/` subdirectory under `panels/` for the four table
  components (`RpcConnectionsDebugTable.tsx` etc.). Visual
  shape and field selection mirrors
  `fxtr_workstation/debug_ui/src/tables/*.tsx` but adapted to
  the new component palette (`DebugRefChip` instead of inline
  short_id text, panel chrome via `tokens.css`).

Modified:

- `shell/DockviewLayout.tsx` — register the new panel kind.
- `shell/WorkstationShell.tsx` — add an `openWorkstationDebugPanel`
  callback paralleling `openRootsPanel`, plus a
  `focusOrOpenWorkstationDebug` analog of `focusOrOpenRoots`,
  threaded into `OpenPanelsSidebar`.
- `sidebar/OpenPanelsSidebar.tsx` — accept and render a new
  `onOpenWorkstationDebug` prop next to `onOpenRoots`. Button
  text: "+ Debug".
- `index.ts` — re-export the new public types.

End-of-chunk verification:

- `pnpm dev` in `fxtr-ui-workstation-demo`. "+ Debug" button
  in sidebar opens the workstation debug panel. Four tables
  populate from the demo backend. Short-ID chips work: click
  opens default view in a new panel; right-click → metadata
  in new panel. Re-clicking "+ Debug" focuses the existing
  panel (no duplicate).

### Chunk 7 — Per-session debug panel + Force-terminate

New stateless dockview panel kind keyed by `sessionId`. For its
lifetime: attaches to the target session (read-only — for
lifecycle observation + terminate), plus subscribes to the raw
session-event and session-debug streams.

Connector additions:

- `connector.connection` is now public (was `private readonly`).
  Debug surfaces consume the wire-level
  `connection.registerSubscription({kind: "session", session_id})`
  and `({kind: "session_debug", session_id})` directly. A
  passthrough wrapper on the connector was prototyped and removed
  — the wrapping didn't earn its keep over consuming the
  underlying subscription with inline `frame.type` narrowing.

New files in
`frontend_lib/packages/fxtr-ui-workstation/src/panels/`:

- `SessionDebugPanelParams.ts` — `{kind: "session_debug",
  sessionId}` + `SESSION_DEBUG_PANEL_COMPONENT` const.
- `SessionDebugPanel.tsx` — dockview content renderer.
  Manages:
  - One `connector.attachSession(sessionId)` for the panel's
    lifetime; used only for `terminate()` and observing
    `lifecycle` (`detach()` on unmount).
  - Two raw subscriptions via
    `connector.connection.registerSubscription({kind: "session", ...})`
    and `({kind: "session_debug", ...})`, state in local
    `useState`/`useReducer` — mirrors the existing
    `SessionSubPanel.tsx` in the debug UI but adapted to the
    new types.
  - A Force-terminate button at the top: calls
    `session.terminate()` (the attached `WorkstationSession`)
    with no confirmation.
  - Sections (top-to-bottom):
    1. Header: session id, seq / ack_seq / slot_counter,
       lifecycle badge, force-terminate button.
    2. **Last state** — collapsible JSON tree
       (`JsonRefTree` from chunk 2) of `last_state.state`,
       with FxtrID leaves rendered via `<DebugRefChip>`.
    3. **Session pins** — small tables of pinned services /
       entities filtered from `connector.listServices()` /
       `listEntities()` by `pinned_by_sessions`, with
       refcount badges from the debug subscription. Short
       IDs as `<DebugRefChip>`.
    4. **Open slots** — three small tables (callbacks,
       invokes, loads) populated from the debug snapshot.
    5. **Event log** — timeline of events with the same
       three-way row coloring as the existing debug UI
       (preserved / past-horizon / unacked-suffix). State
       updates render twice (pending row at ack_seq +
       committed marker at seq). Short IDs in payloads as
       `<DebugRefChip>`.
- Connector tests for the two new subscribe methods, mirroring
  existing connector-level test patterns.

Modified:

- `shell/DockviewLayout.tsx` — register `SessionDebugPanel`.
- `panels/WorkstationDebugPanel.tsx` — wire the "open debug"
  button on each sessions-table row to open a per-session
  debug panel.
- `index.ts` — re-export the new types + connector methods.

End-of-chunk verification:

- `pnpm dev`. Open the workstation debug panel. Click a
  session's "open debug" → per-session debug panel opens.
  All five sections populate; refcounts / preserved events
  update live. Force-terminate works: panel closes (or
  shows "session no longer available"). The dispatcher
  session itself is visible in the workstation debug
  panel and can be debugged the same way (force-terminate
  would leave the shell in a known state — this is the
  user's choice).

### Chunk 8 — Demo service renderers

Hand-written `FullPanel` components for the demo workstation's
services. Lives entirely in
`frontend_lib/packages/fxtr-ui-workstation-demo/src/`.

Modified Python:

- `src/demo_workstation.py` — add a third demo service:
  `notify_n(cb, n: int, prefix: str)` — invokes `cb.notify` n
  times in sequence (same shape as the integration tests'
  `notify_n` in `_demo_workstation.py`). Register under name
  `demo-notify-n` with a fresh type id. Update the `TYPE_IDS`
  JSON emit accordingly.

Modified TS:

- `src/client/dispatch_registry.tsx` — extend `DemoTypeIds` with
  `notify_n`. Add a `service` entry to the registry with
  hand-written FullPanel renderers (and optionally
  BlockLink / HoverPreview for tighter visual integration).
- `src/client/handlers.tsx` — new exports:
  - `EchoServiceFullPanel` — input + "invoke echo" button;
    invokes via `useSessionRootStateAtom` + `allocateInvoke`;
    appends `OpResultStub` to an `invokes: InvokeSlot[]`
    array; each slot renders a result row using `useResult`.
  - `CounterServiceFullPanel` — increment / get buttons,
    same shape.
  - `NotifyNServiceFullPanel` — allocates a callback inbox
    via `ctx.allocateCallback<string>()`, then on invoke
    appends `(handle, n, prefix)` to a slots array. Renders
    the inbox's `events` atom as a live-updating list. Tests
    callback-stream UX end-to-end.

End-of-chunk verification:

- `pnpm dev` in `fxtr-ui-workstation-demo`. From the Roots
  panel, open the echo service → invoke it → see results.
  Same for counter. Notify-n: enter prefix + count, fire,
  watch events stream in.

### Update CODE_OVERVIEW

- `projects/fxtr_workstation/CODE_OVERVIEW.md` — note the
  expanded frontend_lib surface (debug panels, metadata view,
  demo service renderers).
- `projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md` —
  update each affected package section: `@fxtr-ui/workstation`
  (debug panels, metadata view, `DebugRefChip`, connection
  header; `PanelViewEntry` replacing `StubRef`),
  `@fxtr-ui/connector-workstation` (new subscribe methods),
  `@fxtr-ui/workstation-demo` (service renderers + new
  notify-n service).
- `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md`
  — update directory layout + mental-model sections.

## Open subdecisions

To be settled during implementation, not in the plan:

- **Visual treatment of "Metadata:" prefix** on breadcrumb /
  sidebar / history entries. Lock when we see it in context.
- **Direct `connector.terminateSession(id)`**. Initially the
  per-session debug panel does
  `connector.attachSession(id).terminate()` — works, slightly
  indirect. Consider exposing a direct method if the indirection
  becomes painful elsewhere.
- **Reactive wrappers for the session / session-debug subscriptions**
  (i.e. `Reactive*` style with atoms). Initial: debug surfaces
  consume the underlying connection's `registerSubscription`
  directly. Layer reactive on later if multiple consumers
  materialize.

## Cleanup log

Will populate as work progresses. Items spotted during
implementation that we decide to defer for follow-up.

## Skipped from the original stage 9 sketch

- Session attach via UUID input. The localStorage-backed
  dispatcher session id covers the shell-level reattach case;
  per-ref-panel sessions reattach via the persisted layout.
  Standalone UUID entry isn't useful enough to build.
- `example-demo-fingerprinting` rewrite. Outdated and not
  blocking anything in stage 9; revisit if/when it becomes
  relevant.
