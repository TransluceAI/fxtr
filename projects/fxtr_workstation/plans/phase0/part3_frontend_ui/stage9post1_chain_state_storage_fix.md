# Stage 9 post-cleanup 1 — Stub-bearing chain entries + per-entry storage

A correctness refactor of `RefPanelChainState` that fixes a
refcount-GC bug while also making liveness-through-navigation a
property of the data structure rather than an emergent side
effect of which slot happens to be allocated.

## Problem

`RefPanelChainState`'s chain is `ReadonlyArray<PanelViewEntry>`
where `PanelViewEntry = {kind, shortId, display}` — a plain
identifier, not a stub. `collectStubsInState` only matches
`instanceof EntityRefStub | ServiceRefStub`, so chain entries
contribute nothing to Track-op derivation. Entities in the chain
stay alive *only* because something else in user state
incidentally pins them — typically a slot stub allocated by a
renderer.

This breaks visibly with the hardcoded metadata view. The view
allocates `OpResultStub`s into `RefPanelChainState.metadataStorage`
— for a service, `{metadata, ctxInvoke, typeInvoke}`. The
`typeInvoke` stub is the only thing preserving the `$fxtr.type`
invoke event's seq, which is the only thing pinning the type
entity server-side.

The compute* functions in `breadcrumb/history.ts`
(`computeNextChainState` and its four siblings) build their
returned state with only `{chain, activeIndex, historyRoot}` —
they silently drop `ownStorage` and `metadataStorage`. When the
user clicks a ref in the metadata view, `followLink`
(`replace-in-source`) writes the truncated state, the session
reducer's flush walk sees the slots leave user state, and ships
`UnpreserveEventsOp` for their seqs. The server drops the events
from `preserved_events`, retention prune decrefs
`event_refs[seq]`, the entity record's refcount hits zero, the
short ID deactivates. The panel re-renders with the now-dead
entity as its active ref; the dispatcher session's allocate-load
hits `validation_failed: unknown or inactive entity short id`,
and the dispatcher's slot is poisoned in `local.dedup.load`.

The same fragility applies to **`HistoryRoot.branched.prefix`** —
it's `ReadonlyArray<PanelViewEntry>` too. Ancestor entries are
held only by shortId, with no liveness guarantee. A
history-root-menu click that fires long after the original branch
can find its entities already GC'd.

## Design

Three coordinated changes:

1. **Stub-bearing in-process types.** `PanelViewEntry` and
   `Breadcrumb` carry live, connector-interned stubs. Because
   chain entries and history-root prefixes live in session
   userState, `collectStubsInState` walks them and the reducer
   emits `TrackEntityOp` / `TrackServiceOp` for every entity /
   service currently reachable from the panel's navigation
   structure. Liveness becomes correctness-by-construction.

2. **Wire-safe sister types for drag.** Drag payloads serialize
   through `JSON.stringify`, which would lose stub class
   identity. We split off `PanelViewDragData` / `BreadcrumbDragData`
   as the JSON-safe shapes used by `setDragData` / `parseDragData`.
   `keyOfEntry` / `entryOfDragData(connector)` mediate between
   them.

3. **Per-entry renderer storage.** Today `RefPanelChainState`
   carries singleton `ownStorage?` and `metadataStorage?` slots
   shared across all chain entries — the active renderer reads
   and writes to whichever one matches its display mode.
   Replace them with a single `storages` array parallel to
   `chain`: `storages[i]` belongs exclusively to `chain[i]`,
   regardless of which entry is currently active. This gives
   each chain entry a stable home for its renderer state that
   survives navigating away and back, and removes the implicit
   one-renderer-at-a-time constraint where switching display
   modes inside a panel would have to either share or stomp the
   same slot. With liveness now anchored on stubs in chain, the
   `storages` array is a pure cache — its contents can be
   cleared wholesale (e.g. to recover memory or force a
   re-allocate) without breaking anything else.

### Types

```ts
// In-process entry — carries a live stub. Used in chain,
// historyRoot.prefix, breadcrumb trails, and the
// `WorkstationApi.followLink` event's `target`.
interface PanelViewEntry {
    readonly stub: EntityRefStub | ServiceRefStub;
    readonly display: "default" | "metadata";
}

// Wire-safe identifier — used in drag payloads and anywhere else
// that has to round-trip through plain JSON. Re-interns to a
// PanelViewEntry on the receiving side via the connector.
interface PanelViewDragData {
    readonly kind: "entity" | "service";
    readonly shortId: string;
    readonly display: "default" | "metadata";
}

// In-process breadcrumb — references PanelViewEntry on `from`/`to`.
interface Breadcrumb {
    readonly from?: PanelViewEntry;
    readonly to: PanelViewEntry;
    readonly via: BreadcrumbVia;
}

// Wire-safe breadcrumb — references PanelViewDragData on `from`/`to`.
interface BreadcrumbDragData {
    readonly from?: PanelViewDragData;
    readonly to: PanelViewDragData;
    readonly via: BreadcrumbVia;
}

// Conversion helpers:
function dragDataOfEntry(entry: PanelViewEntry): PanelViewDragData;
function entryOfDragData(
    data: PanelViewDragData,
    connector: WorkstationConnector,
): PanelViewEntry;
// And similarly dragDataOfBreadcrumb / breadcrumbOfDragData.
```

`dragDataOfEntry` reads `kind` via `entry.stub instanceof EntityRefStub`.
`entryOfDragData` re-interns via `connector.entityRef(shortId)` /
`connector.serviceRef(shortId)`. Refs are connector-scoped (no
per-session state), so two interns of the same shortId hand back
the same instance — drag round-trips losslessly within a window.

```ts
interface HistoryRoot {
    readonly kind: "roots";
}
// or:
interface HistoryRootBranched {
    readonly kind: "branched";
    readonly prefix: ReadonlyArray<PanelViewEntry>;  // stub-bearing
    readonly parent: HistoryRoot;
}
```

```ts
interface RefPanelChainState {
    readonly chain: ReadonlyArray<PanelViewEntry>;       // stub-bearing
    readonly activeIndex: number;
    readonly historyRoot: HistoryRoot;
    // Parallel to chain. `storages[i]` is whatever the renderer
    // for `chain[i]` chose to put there — opaque from the shell's
    // POV. Optional per-entry; renderers narrow via atomWithDefault.
    // Purely a cache: chain stubs are what keep entities alive.
    readonly storages: ReadonlyArray<unknown>;
}
```

Invariant: `chain.length === storages.length`. Both arrays advance
in lockstep.

### Liveness story

When `followLink` writes a new chain state into the panel's
session userState:

- `collectStubsInState` walks the value and collects every
  `EntityRefStub` / `ServiceRefStub` instance — including the
  stubs in `chain`, the stubs in `historyRoot.branched.prefix`
  (recursively), and any stubs the renderer left in `storages`.
- The session reducer's flush diffs the collected stubs against
  the previous committed set, emitting `TrackEntityOp` /
  `TrackServiceOp` for newcomers and `UntrackEntityOp` /
  `UntrackServiceOp` for those that left.
- All ops ride in a single `MutateSessionRequest`. The server's
  `apply_mutation` runs tracker ops in the commit phase *before*
  retention prune, so a chain that simultaneously appends a new
  stub and drops an old slot's `OpResultStub` results in: Track
  the new stub (incref), Unpreserve the old seq (decref the
  preserved-event contribution), retention prune (decref the
  event_ref). The entity's refcount never dips to zero because
  the Track ref takes over before the preservation ref leaves.

This is the correctness-by-construction property: any entity the
user can still navigate to (via chain back-nav or via the
history-root menu) is necessarily Track-pinned by the panel
session.

### Storages as cache

Once liveness is anchored on chain stubs, `storages` is a pure
performance optimization:

- A renderer that lazily clears its own entry's storage doesn't
  break anything — the entry's stub is still in `chain`, still
  Track-pinned.
- Trimming old storages (e.g. keep only active ± N) is a safe
  memory-bound knob.
- Re-mounting an entry after `storages[i]` was cleared just
  re-allocates — invokes re-fire (no dedup), metadata / loads
  hit their wire-visible dedup map and reuse existing slots.

Storage propagation in compute* functions:

- `computeNextChainState`:
  - Identity / forward-by-one / jump-to-existing: keep `chain`
    and `storages` unchanged; only `activeIndex` moves.
  - Append: truncate both arrays to `activeIndex+1`, then
    append the stub-bearing `target` to `chain` and `undefined`
    to `storages`.
- `computeOpenNewFromLink`, `computeSnapshotFromTrail`,
  `computeOpenNewFromHistoryEntry`: chains built fresh from
  trails / history entries (which already carry stubs in the new
  shape). `storages` is a fresh `undefined`-filled array of
  matching length.
- `computeOpenNewFromChainEntry`: parallel slice across `chain`
  and `storages`. (Stubs in the slice are connector-interned —
  safe to share into the new session's userState.)

### Drag boundary

`setDragData` calls `dragDataOfEntry` / `dragDataOfBreadcrumb`
to lower the payload to wire-safe types before stringifying.
`parseDragData` returns the wire-safe types directly — call
sites (currently only in the shell's drop handler) call
`entryOfDragData(_, connector)` to lift back to live entries
before applying.

`LinkDragPayload` / `SnapshotDragPayload` get retyped over the
drag-data shapes:

```ts
interface LinkDragPayload {
    kind: "link";
    target: PanelViewDragData;
    trail: BreadcrumbDragData[];
    sourcePanelId?: string;
}

interface SnapshotDragPayload {
    kind: "snapshot";
    // Wire-safe chain summary. Drop reconstitutes a full
    // RefPanelChainState by interning stubs through the
    // connector. Drop also fills storages with `undefined`s —
    // OpResultStubs in the source's storages belong to the
    // source's session and would substitute to broken-sentinels
    // anyway.
    chain: ReadonlyArray<PanelViewDragData>;
    activeIndex: number;
    historyRoot: HistoryRootDragData;   // recursive: prefix is PanelViewDragData[]
    target: PanelViewDragData;
    sourcePanelId?: string;
}
```

The shell's drop handler runs the lift on the way in and
constructs a `RefPanelChainState` with re-interned stubs and
`storages = chain.map(() => undefined)`.

## Status

Chunk progress:

- [x] Chunk 1 — Stub-bearing `PanelViewEntry` + drag-data split
- [x] Chunk 2 — Per-entry `storages` array
- [x] Chunk 3 — Layout migration + CODE_OVERVIEW updates

## Chunks

### Chunk 1 — Stub-bearing `PanelViewEntry` + drag-data split

The load-bearing fix. After this chunk lands the original GC
bug is closed (chain stubs alone are enough — see "Liveness
story" above). The singleton `ownStorage` / `metadataStorage`
fields stay on `RefPanelChainState` for now; the compute*
functions keep stripping them on navigation, exactly as today.
That's deliberate — the regression they caused was the entity
GC, which chain Track ops now cover; the secondary
quality-of-life issue (re-allocate on back-nav to a metadata
view) is what chunk 2 addresses.

Files touched (paths under
`frontend_lib/packages/fxtr-ui-workstation/src/`):

- `types.ts` — redefine `PanelViewEntry` as `{stub, display}`;
  add `PanelViewDragData = {kind, shortId, display}` (the old
  shape). Helpers: `panelViewEntryOf(stub) → PanelViewEntry`
  (default-display), `dragDataOfEntry(entry) → PanelViewDragData`,
  `entryOfDragData(data, connector) → PanelViewEntry`. Keep
  `StubKind` / `PanelViewDisplay` literal types.

- `breadcrumb/types.ts` — `Breadcrumb.from?` / `to` reference
  stub-bearing `PanelViewEntry`. Add `BreadcrumbDragData`
  mirror plus `dragDataOfBreadcrumb` /
  `breadcrumbOfDragData(connector)`. Update
  `HistoryRoot.branched.prefix` to
  `ReadonlyArray<PanelViewEntry>` (stub-bearing). `buildTrail`
  signature unchanged.

- `breadcrumb/history.ts` — `sameRef` compares `stub.shortId`
  + stub kind (`instanceof EntityRefStub`) + `display`. compute*
  functions thread stubs through their `target` / `trail` /
  segment-prefix inputs naturally. Continue to strip
  `ownStorage` / `metadataStorage` here; chunk 2 swaps them for
  the array.

- `shell/WorkstationApi.ts` — `FollowLinkEvent.target` is
  stub-bearing `PanelViewEntry`. `restoreIntoNewPanel`'s state
  carries stub-bearing chain.

- `reflink/useRefClick.ts`, `useRefContextMenu.tsx` — build
  target as `{stub, display}` via `panelViewEntryOf(stub)` (+
  display override).

- `reflink/drag.ts` — `LinkDragPayload` / `SnapshotDragPayload`
  retyped over `PanelViewDragData` / `BreadcrumbDragData`.
  `setDragData` lowers via `dragDataOfEntry` /
  `dragDataOfBreadcrumb` before `JSON.stringify`. `parseDragData`
  returns the wire-safe shapes.

- `reflink/useDraggableRef.ts`, `useDraggableChainEntry.ts`,
  `useDraggableHistoryEntry.ts` — payload builders source
  stub-bearing entries; conversion happens at the `setDragData`
  boundary in `drag.ts`.

- `shell/DockviewLayout.tsx` — drop handler lifts via
  `entryOfDragData` / `breadcrumbOfDragData(connector)` before
  calling `followLink` / `restoreIntoNewPanel`. Confirm the
  connector is in scope; plumb via prop if not.

- `panel/PanelWrapper.tsx` — read active stub directly from
  `chain[activeIndex].stub`; drop the
  `connector.entityRef(activeStub.shortId)` reconstruction.

- `breadcrumb/BreadcrumbBar.tsx`, `BreadcrumbEntry.tsx`,
  `HistoryRootEntry.tsx` — read `entry.stub` for rendering /
  passing into `RefLink` / `RenderDispatch`.

- `sidebar/OpenPanelsSidebar.tsx`, `SidebarTooltip.tsx`,
  `usePanelLayout.ts` — read `chain[activeIndex].stub.shortId`
  for labels; read `chain[activeIndex].stub` for the row's
  `RefLink`.

End-of-chunk verification:

- Type-check + biome pass.
- `pnpm dev` in `fxtr-ui-workstation-demo`. Open a service
  metadata panel; click the `$fxtr.type` ref. Entity panel
  loads cleanly (no `validation_failed`). Open the workstation
  debug panel; the type entity is in `entities` with the panel
  session in `pinned_by_sessions`.
- Drag a breadcrumb entry into a new tab; the new panel opens
  with a stub-bearing chain entry — Track op fires in its
  session (verify via workstation debug panel).
- History-root-menu click on a deep ancestor still loads
  cleanly even after navigating away long enough that the
  source's slot pins would have lapsed.

### Chunk 2 — Per-entry `storages` array

Replace singleton `ownStorage` / `metadataStorage` with one
`storages: ReadonlyArray<unknown>` parallel to `chain`.
Renderer state survives back-navigation; storage becomes a
pure cache.

Files touched (paths under
`frontend_lib/packages/fxtr-ui-workstation/src/`):

- `breadcrumb/types.ts` — drop `ownStorage?` / `metadataStorage?`
  from `RefPanelChainState`; add required
  `storages: ReadonlyArray<unknown>` with invariant doc
  (`chain.length === storages.length`).

- `breadcrumb/history.ts` — compute* fns maintain `storages` in
  lockstep with `chain`:
  - `computeNextChainState`: identity / forward / jump-to-
    existing keep `storages` unchanged; append truncates both
    arrays to `activeIndex+1` and appends `undefined` to
    `storages` alongside `target` to `chain`.
  - `computeOpenNewFromLink`, `computeSnapshotFromTrail`,
    `computeOpenNewFromHistoryEntry`: `storages` is
    `chain.map(() => undefined)`.
  - `computeOpenNewFromChainEntry`: parallel slice across
    `chain` and `storages`.

- `panel/PanelWrapper.tsx` — single storage projection above
  the empty-chain early return (rule-of-hooks):
  ```ts
  const storagesAtom = useSubAtom(userStateAtom, "storages");
  const storageAtom = useSubAtom(storagesAtom, activeIndex);
  ```
  Pass `storageAtom` as the single storage prop to whichever
  renderer the branch picks. The atom at `activeIndex === -1`
  is never read (early return).

- `metadata/EntityMetadataView.tsx`, `ServiceMetadataView.tsx`
  — accept the renamed prop if we go with `ownStorage` for
  symmetry; otherwise keep `storage`. See open subdecision.

- `shell/WorkstationShell.tsx` — `DEFAULT_REF_PANEL_CHAIN_STATE`
  gains `storages: []`; any seed-state construction in
  `openNewPanel` / `mintPanelSession` ensures storages length
  matches chain length.

- `shell/DockviewLayout.tsx` — drop handler's reconstituted
  `RefPanelChainState` fills
  `storages: chain.map(() => undefined)`.

End-of-chunk verification:

- Type-check + biome pass.
- `pnpm dev` in `fxtr-ui-workstation-demo`. Open service
  metadata; click `$fxtr.type`. Back-navigate via breadcrumb;
  the service-metadata sections re-render from existing
  `OpResultStub`s without re-invoking — verify via the
  workstation debug panel that no fresh `ctxInvoke` /
  `typeInvoke` slots open.
- Truncation: forward-nav past a chain entry, then navigate
  to a *different* target from the still-active entry. Future
  is truncated; previously-tracked entities decref via
  `UntrackEntityOp`; storages drop in lockstep.

### Chunk 3 — Layout migration + CODE_OVERVIEW updates

Files touched:

- `frontend_lib/packages/fxtr-ui-workstation/src/persistence/layout_v5.ts`
  — rename to `layout_v6.ts`; bump `LAYOUT_STORAGE_VERSION`
  constant. Update import paths everywhere it's referenced
  (`WorkstationShell.tsx`, `useLayoutPersistence.ts`, any
  others).

- `projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md` —
  `@fxtr-ui/workstation` section: describe stub-bearing
  `PanelViewEntry`, the `PanelViewDragData` split, the
  `storages` array, the Track-via-chain liveness guarantee.

- `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md`
  — update directory layout, mental-model section, and any
  worked-example pseudocode that touched the chain state shape.

End-of-chunk verification:

- Old persisted v5 layout in localStorage clears cleanly on
  first load; a new v6 layout persists across a refresh.

## Open subdecisions

- **`HistoryRoot` wire shape on drag**: simplest is a recursive
  `HistoryRootDragData` mirror. Verify the existing snapshot
  drag path actually serializes historyRoot — if not, we can
  skip the mirror type.
- **`sameRef` derivation**: derive `kind` via `instanceof` or
  cache as a parallel field on `PanelViewEntry`? Probably
  `instanceof` — one extra check, no schema duplication.

## Cleanup log

Will populate as work progresses.
