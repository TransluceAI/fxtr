# Stage 9 scratch — design notes

Working notes that informed the stage 9 plan rewrite. Captures the
decisions made during the alignment chat so they're not lost when
the plan itself slims them down to load-bearing facts.

## Decisions made during alignment

- **`StubRef` → `PanelViewEntry` (rename + extend).** Find-replace
  the type name; add a required `display: "default" | "metadata"`
  field. Every existing site that holds a `StubRef` is one of:
  chain entry, drag target, breadcrumb segment, history-root
  prefix entry, follow-link target. All of these are "a thing you
  can open in a panel", and they all benefit from carrying the
  display mode — drag a metadata view into a new tab; breadcrumb
  shows the metadata flavor of a ref distinctly; history-root
  restores into the same mode you were in before branching.
- **Required, not optional, `display`.** Wire overhead is
  negligible; equality logic stays straightforward (no
  `?? "default"` everywhere); diffs against current chain code
  surface every site that needs to think about display mode.
- **`sameRef` compares `display`.** So `(ref-X, default)` and
  `(ref-X, metadata)` are distinct entries — clicking "Open
  metadata here" on the currently-active ref appends a new entry
  rather than no-op'ing.
- **Workstation debug panel = stateless dockview panel** like
  Roots. Opened via a `+ Debug` button next to `+ Roots` in the
  sidebar header; find-or-focus-or-open behavior. Subscribes to
  all four resource lists with `debug=true`; shows the four
  tables; each session row has an "open session debug" button.
- **Per-session debug panel = its own dockview panel kind**
  (`{kind: "session_debug", sessionId}`). Doesn't fit the chain
  model. Stateless w.r.t. its own session; just attaches to the
  target session for the panel's lifetime. Carries a Force-
  terminate button (no confirmation — user is already in the
  debug view).
- **Debug-panel short-ID chips: a `DebugRefChip` component.**
  Renders a plain-text monospace chip (no dispatch). Wired up
  with the same drag / click / right-click hooks as `RefLink`,
  but with `<PanelContext.Provider value={null}>` around them so
  clicks always route through `restoreIntoNewPanel`. Click →
  opens the *default* view in a new panel. Right-click offers
  both "Open in new panel" and "Open metadata in new panel".
  No `HoverPreview` dispatch (would track in dispatcher session).
- **Metadata view: a hardcoded `MetadataView` component**
  rendered by `PanelWrapper` when the active entry has
  `display: "metadata"`, bypassing the `FullPanel` dispatch.
  Two variants:
  - Entity: `allocateGetEntityMetadata` for metadata fields;
    `allocateLoad` for raw data; raw data shown as a collapsible
    JSON tree with `FxtrID` leaves rendered as
    `<RefLink as="inline">`; the `$fxtr.ctx` and `$fxtr.type`
    top-level fields of the loaded data are pulled out and
    displayed at the top.
  - Service: `allocateGetServiceMetadata` for metadata; invoke
    `$fxtr.ctx` and `$fxtr.type` as method names via
    `allocateInvoke(stub, "$fxtr.ctx", [])` /
    `allocateInvoke(stub, "$fxtr.type", [])` and display the
    results.
- **Right-click menu** gets two new items (alongside the existing
  "Open in new panel"):
  - "Open metadata in new panel" — always available
  - "Open metadata here" — only when inside a ref panel; replaces
    in source per the standard chain rules but with
    `display: "metadata"`
  And the existing "Open in new panel" stays. We optionally
  add "Open here" too for symmetry (currently only clicks /
  drag-to-same-panel do replace-in-source). I'll do this so the
  metadata flavor isn't the *only* way to get the menu-driven
  replace-in-source behavior.
- **Connection status indicator** lives in the existing top
  header (the one that conditionally renders the debug-UI link).
  Always-on now: shows a colored dot + status text + workstation
  URL + optional debug-UI link. Visual style follows
  `fxtr-ui-demo`'s `ConnectionStatusBar`.
- **Demo service renderers** fill in the service side of
  `fxtr-ui-workstation-demo/src/client/dispatch_registry.tsx`.
  Echo / counter get hand-written FullPanel components with
  invoke buttons and result lists. A new `notify_n`-style
  service is added to the demo Python backend so we can
  demonstrate callback inbox display in a FullPanel too.

## Architectural ripples from the refactor

`StubRef` is used in 30+ places. The find-replace itself is
mechanical, but the equality / rendering paths need surgery:

- `breadcrumb/history.ts::sameRef` — compare `display` too.
- `breadcrumb/BreadcrumbEntry.tsx` — prepend "Metadata:" or
  similar visual marker for `display === "metadata"`.
- `breadcrumb/HistoryRootEntry.tsx` — same.
- `sidebar/OpenPanelsSidebar.tsx::RefSidebarRow` — same; the
  "active stub" of a panel can now be a metadata entry.
- `sidebar/SidebarTooltip.tsx` — decide what to show for
  metadata entries (probably skip dispatch and show plain text).
- Drag preview rendering — if we use the existing draggable hook
  it just sets data; the visible drag image is browser-default,
  no metadata-aware change needed.
- `PanelWrapper.tsx` — branch on `activeEntry.display` before
  rendering `RenderDispatch`.

## Open subdecisions left to implementation

- Exact visual treatment of "Metadata:" prefix on breadcrumb /
  sidebar / history entries — settle when we see them rendered.
- Whether to add a `connector.terminateSession(id)` direct method,
  or do the panel-side `attachSession(id).terminate()` pattern.
  Going with the latter initially.
- Whether `subscribeSessionEvents` / `subscribeSessionDebug`
  exposure on the connector should be raw passthroughs or
  reactive (`Reactive*` style with atoms). Initial: raw
  callback-based; reactive can layer on later if useful.

## Skipped from original stage 9

- Session attach affordance via a UUID input. The
  localStorage-backed dispatcher session id handles the reattach
  case for the shell; per-ref-panel sessions reattach via the
  persisted layout. No standalone UUID entry needed.
- `example-demo-fingerprinting` rewrite — outdated; revisit
  separately if/when it becomes relevant again.
