# Step 6 — Per-panel breadcrumb history

The navigate callback already emits a `Breadcrumb[]` trail but the shell
just `console.log`s it. Step 6 turns every entity panel into a stateful
history container: a chain of previously-viewed entities with an active
position, plus a collapsed "history root" describing how the panel
itself was reached. All link-click / drag / right-click / breadcrumb
interactions now reshape this state instead of just swapping
`entityIdString`.

## Goal

Let the user navigate deeply through linked entities without panel
proliferation — but still be able to fork off sibling investigations
and trace their way back up. In practice:

- Clicking a ref extends (or rewinds into) the current panel's chain.
- Back/forward via clicks on breadcrumb entries.
- Drag / right-click → branch: new panel gets its own chain, and the
  source panel's state-at-branch becomes the new panel's history root.
- A history root icon at the top-left of the panel unrolls the full
  ancestor chain; clicking or dragging one of those entries "time-
  travels" by opening a new panel positioned at that ancestor state.

## Data model

### Panel state (stored directly in dockview panel params)

```ts
interface EntityPanelParams {
  chain: string[];          // entity idStrings, length >= 1
  activeIndex: number;      // 0..chain.length-1
  historyRoot: HistoryRoot;
}

type HistoryRoot =
  | { kind: "namespace" }
  | { kind: "branched"; prefix: string[]; parent: HistoryRoot };
```

- `chain` is the linear list of entities this panel has visited.
  `chain[activeIndex]` is the entity currently rendered by the
  `FullPanel`.
- `historyRoot` is recursive: each `branched` node holds the prefix of
  the *parent panel's* chain up to its own active-at-branch position,
  plus that parent panel's own `historyRoot`. The base case is
  `{ kind: "namespace" }`.

Chain entries and history-root prefixes both store *id strings* (not
FxtrIDs) so the whole shape is JSON-round-trippable through dockview's
`toJSON` / `fromJSON`.

### Why this shape

- Everything a panel needs to render its breadcrumb UI lives in its
  params. No shell-side registry of "per-panel state" for the render
  path.
- The recursive `historyRoot` preserves branch boundaries: clicking
  "D" in `[[ns]>A>B]>[C>D]>[E]` yields `[[ns]>A>B]>[C>D]` — we don't
  flatten then re-split, we just peel layers off. See
  "History-root-menu click" below.

## Interaction → state transformations

All reshaping logic lives in a single pure module:
`src/breadcrumb/history.ts`. Each of the transformations below is a
named function that takes the relevant inputs and returns a new
`EntityPanelParams`.

### Click a link in panel/hover content — `replace-in-source`

Inputs: source panel state `{chain, activeIndex, historyRoot}`, target
entity id.

```
if target === chain[activeIndex]:           no-op
elif target === chain[activeIndex + 1]:     activeIndex += 1
elif target appears at some index i > 0:    activeIndex = i     (keep future past i)
else:                                       chain = chain[..activeIndex+1] ++ [target]
                                            activeIndex = len(chain) - 1
```

Fourth branch is the normal "follow a new link" case. Second branch is
the "pressed forward" shortcut (user re-clicks the same next entry).
Third matches the requested "already anywhere in chain → jump, don't
mutilate" behavior (`(b)` from the Q&A: future past the jumped-to
position stays intact, mirroring browser back/forward).

### Click a link in panel/hover content — `open-new`

Inputs: source panel state, navigation trail `Breadcrumb[]`, target
entity id.

```
newChain      = trail.map(t => t.toEntityId)   // assumes target is trail's last `to`
newActive     = newChain.length - 1
newHistRoot   = { kind: "branched",
                  prefix: chain.slice(0, activeIndex + 1),
                  parent: historyRoot }
```

If the source is missing (e.g. `sourcePanelId` was a namespace row) the
trail's only `to` is the target and `newHistRoot` defaults to
`{ kind: "namespace" }`.

The trail already encodes the "beyond-active" path (click inside
`FullPanel` → one segment; click inside a nested hover → one segment
per hover step). Extracting `to`s in order gives the new panel's
chain, which matches the user's worked example (A>B>C panel; drag F
from a hover-E-inside-hover-D-inside-C stack → new chain D>E>F,
history root A>B>C).

### Click a breadcrumb entry (chain entry `i`)

Pure activation: `activeIndex = i`. Chain and historyRoot unchanged.
No navigation event is emitted. Cheap, same semantics as sidebar row
click.

### Drag or right-click "Open in new panel" on a breadcrumb entry

Per the Q&A, option (c): the new panel is a rewound copy of *this*
panel — same historyRoot, chain truncated up to the chosen entry.

```
newChain      = chain.slice(0, chainIndex + 1)
newActive     = chainIndex
newHistRoot   = historyRoot                    // unchanged
```

This is the same shape as dragging an entry out of the history-root
menu (see below), making "breadcrumb vs history root" a purely visual
distinction at drag time.

### Click or drag from the history-root menu

The menu renders a flat list (with subtle separators between segments)
built by walking the recursive `historyRoot` outward-in. Each flat
entry carries:

```ts
interface FlatHistoryEntry {
  entityIdString: string;
  segmentIndex: number;     // 0 = deepest (nearest namespace)
  positionInSegment: number;
  // lazy-computed lookups:
  segmentPrefix: string[];
  segmentParent: HistoryRoot;
}
```

Opening an entry:

```
newChain      = segmentPrefix.slice(0, positionInSegment + 1)
newActive     = positionInSegment
newHistRoot   = segmentParent
```

This is what yields the unflattened shape the user called out: clicking
D in `[[[ns]>A>B]>[C>D]]>[E]` gives `[[ns]>A>B]>[C>D]`, not a flattened
`[ns]>A>B>C>D`. The preservation falls out naturally because we
reconstruct from `segmentParent`, not from the flat list.

**Invariant:** navigation from the history-root menu *always* produces
a new panel, never `replace-in-source`. The API makes this obvious-by-
construction — the history-root path uses `restoreIntoNewPanel`, which does not
take an intent (see below).

## Navigation API redesign

The single-entry-point `navigate(event)` collapses too many semantics.
Step 6 splits the workstation API into three methods that each have
one job:

```ts
interface WorkstationApi {
  // Clicks on refs inside content. Handles both replace-in-source
  // (chain mutation) and open-new (derives newChain from trail +
  // source state).
  followLink(event: FollowLinkEvent): void;

  // Cheap in-place activation. Just shifts activeIndex of one panel.
  // No chain change, no new panel, no breadcrumb emission.
  activateChainEntry(panelId: string, chainIndex: number): void;

  // Reopen a fully-resolved state snapshot in a fresh panel. No
  // intent, no trail — the caller (breadcrumb-entry drag, history-
  // root menu) already has every field of the new panel's params.
  // Never mutates an existing panel.
  restoreIntoNewPanel(params: EntityPanelParams, position?: DropPosition): void;
}
```

- `FollowLinkEvent` is the current `NavigateEvent` shape. Keeps
  `target`, `trail`, `sourcePanelId`,
  `intent: { kind: "replace-in-source" } | { kind: "open-new"; position? }`.
- `activateChainEntry` could just `api.updateParameters(...)` directly
  from the breadcrumb-bar component, but putting it on the workstation
  API keeps call sites consistent and gives us one place to add
  side-effects (telemetry, future breadcrumb card, etc).
- `restoreIntoNewPanel` takes a fully-computed `EntityPanelParams` because the
  caller (breadcrumb-entry drag, history-root menu) already has
  everything needed — there's no trail to apply.

### Which method each interaction calls

| Interaction                                   | Method                   |
| --------------------------------------------- | ------------------------ |
| Click `EntityLink` in content                 | `followLink(replace)`    |
| Right-click `EntityLink` → "Open in new panel" | `followLink(open-new)`   |
| Drag-drop `EntityLink` onto dockview          | `followLink(open-new)` (with drop position) |
| Click a breadcrumb-bar entry (chain item)     | `activateChainEntry`     |
| Right-click a breadcrumb-bar entry → open new | `restoreIntoNewPanel`              |
| Drag-drop a breadcrumb-bar entry              | `restoreIntoNewPanel`              |
| Click a history-root menu entry               | `restoreIntoNewPanel`              |
| Drag-drop a history-root menu entry           | `restoreIntoNewPanel`              |
| Click a namespace pin row                     | `restoreIntoNewPanel` (explicit; no panel context) |
| Right-click / drag a namespace pin row         | `restoreIntoNewPanel`                              |

### Why this is cleaner

The split is semantic, not mechanical:

- **`followLink`** is the only method called when a user has *followed
  a link* — a ref in content that carries a trail from some source
  entity to a target. Click / right-click / drag-drop of an
  `EntityLink` all fit this, even when the drop lands in a new panel.
  `followLink` owns all chain-mutation logic (`replace-in-source`) and
  the trail-to-chain derivation for link-based `open-new`.
- **`restoreIntoNewPanel`** is the only method called when the user has *grabbed
  a pre-existing state snapshot* — a breadcrumb entry or a history-
  root menu item. There's no trail; the caller already knows the
  final state. `restoreIntoNewPanel` never mutates an existing panel.
- **`activateChainEntry`** is the only method that never creates a
  panel and never changes an entity's chain — it just shifts
  activeIndex.

Each method's capability is visible in its name; no call site
accidentally opens a panel when it meant to replace, or vice versa.

## Drag payload redesign

Drags split along the same `followLink` vs `restoreIntoNewPanel` boundary as
clicks. A content `EntityLink` drag *is* a link-follow (just with a
drop position); a breadcrumb or history-root entry drag is a state
snapshot. The drag payload is discriminated so the drop handler can
route to the right API method.

```ts
type DragPayload = LinkDragPayload | SnapshotDragPayload;

interface LinkDragPayload {
  kind: "link";
  entityIdString: string;       // target
  trail: Breadcrumb[];          // captured at drag-start, same as click
  sourcePanelId?: string;
}

interface SnapshotDragPayload {
  kind: "snapshot";
  newPanelState: EntityPanelParams;   // fully resolved at drag-start
  entityIdString: string;             // = chain[activeIndex]; UX niceties
  sourcePanelId?: string;
}
```

The dockview drop handler:

```
onDidDrop(e):
  payload = parseDragData(e.dataTransfer)
  if not payload: return
  if payload.kind === "link":
    workstationApi.followLink({
      target:        FxtrID.fromIdString(payload.entityIdString),
      trail:         payload.trail,
      sourcePanelId: payload.sourcePanelId,
      intent:        { kind: "open-new", position: dropPosition(e) },
    })
  else:
    workstationApi.restoreIntoNewPanel(payload.newPanelState, dropPosition(e))
```

### How each drag source builds its payload

- **`useDraggableEntity` (content ref):**
  - Inside a `PanelContext` (the usual case): packs a `LinkDragPayload`
    with target id, `buildTrail(source, entity)`, `sourcePanelId`.
    `followLink` derives the new chain at drop time from the source-
    panel state. This keeps drag and right-click symmetric (both call
    `followLink(open-new)` with the same event shape).
  - No `PanelContext` (e.g. `EntityLink` inside a namespace-panel row
    or inside a hover attached to one): packs a `SnapshotDragPayload`.
    The snapshot is built from the trail via
    `computeSnapshotFromTrail(trail, entity, { kind: "namespace" })` —
    a tiny helper in `history.ts` that turns a non-panel-origin trail
    into a fresh `EntityPanelParams`.
- **`useDraggableChainEntry` (new, breadcrumb entry):** has
  `(panelState, chainIndex)` in scope. Pre-computes
  `newPanelState = computeOpenNewFromChainEntry(panelState, chainIndex)`,
  packs a `SnapshotDragPayload`.
- **`useDraggableHistoryEntry` (new, history-root menu):** has
  `(panelState, flatEntry)` in scope. Pre-computes
  `newPanelState = computeOpenNewFromHistoryEntry(panelState, flatEntry)`,
  packs a `SnapshotDragPayload`.

Right-click "Open in new panel":

- On a content `EntityLink` inside a `PanelContext` (via
  `useEntityContextMenu`) → `followLink({ ..., intent: { kind: "open-new" } })`
  (no position).
- On a content `EntityLink` with no `PanelContext` (namespace-panel
  rows, or refs inside their hovers) → `restoreIntoNewPanel(computeSnapshotFromTrail(...))`.
- On a breadcrumb entry → `restoreIntoNewPanel(computeOpenNewFromChainEntry(...))`.
- History-root entries don't offer right-click (already inside a menu
  — see `HistoryRootMenu.tsx` above).

### Non-panel origins (namespace pins, etc.)

`followLink` is defined to operate on a *source entity panel* — if
there is no `sourcePanelId` pointing at a real entity panel, it has
nothing to replace and no chain to derive from. Rather than making it
fall through to `restoreIntoNewPanel` internally (the old hack from
when `navigate` was a single entry point), we push the routing into
the interaction hooks:

- `useEntityClick`, `useEntityContextMenu`, and `useDraggableEntity`
  all branch on `usePanelContext()`:
  - Panel context present → the normal `followLink` path.
  - No panel context → build a snapshot from the trail via
    `computeSnapshotFromTrail(trail, target, historyRoot)` and route
    through `restoreIntoNewPanel` (click / right-click) or a
    `SnapshotDragPayload` (drag).
- The namespace panel doesn't have to do anything special — pin rows
  are ordinary `<EntityLink>`s, and the fact that they're *not* inside
  a `PanelContext` is what drives the right behavior.
- `historyRoot` defaults to `{ kind: "namespace" }` for non-panel
  origins. If we later add other top-level surfaces (e.g. a search
  panel, a recents panel), they can provide their own context-value
  with a different historyRoot stamp.

`followLink`'s contract becomes crisp: **it is only called when a
source entity panel exists**, and its implementation can assume that
without a fallback.

### Why this split (vs. pre-computing state for drags too)

The earlier revision of this plan had all drag sources pre-compute
state and funnel through `restoreIntoNewPanel`. That made the drop handler
uniform but split the `EntityLink` "open-new" code path in two —
right-click went through `followLink`, drag went through `restoreIntoNewPanel`
— even though the user semantics are identical. Keeping
`EntityLink`-based drags in `followLink` matches click/right-click
and means `followLink` owns *all* link-follow state computation
(see `computeOpenNewFromLink` in `history.ts`). Snapshot drags stay
in `restoreIntoNewPanel` because there's genuinely no trail to apply — they're
restoring a known state.

## Component + context changes

### `workstation/PanelContext.tsx` — extended

Entity panels publish their *live* state so drag hooks can read chain
+ activeIndex + historyRoot at drag start:

```ts
interface PanelContextValue {
  panelId: string;
  kind: "entity";
  chain: string[];
  activeIndex: number;
  historyRoot: HistoryRoot;
}
```

`EntityPanelWrapper` builds this from the params it already reads, so
no new plumbing. Non-entity surfaces still publish `null` (the
namespace panel intentionally omits the provider — unchanged).

Downstream callers that currently read only `panelId` from the context
keep working; new readers (drag hooks) also pull chain/active from the
same hook.

### `EntityPanelWrapper.tsx` — breadcrumb bar + new params

- Reads richer params. Hydrates `activeEntity = FxtrID.fromIdString(chain[activeIndex])`.
- Provides the extended `PanelContext`.
- New top-of-panel bar: `<BreadcrumbBar />` above the existing
  `<Render as={FullPanel}>`. Bar is a fixed row inside the panel's
  `fw-panel` container — scroll happens inside the FullPanel body
  below it.
- Keeps the `key={entity.idString()}` remount trick on the inner
  `<Render>` so `activateChainEntry` (and chain mutations) trigger a
  clean FullPanel remount.
- `scope.lift` / `isTracked` pre-check still runs for the *active*
  entity. Non-active chain entries aren't loaded by the FullPanel —
  the breadcrumb bar lazy-loads their `InlineLink` display via
  `<Render>` with its own `Suspense`.
- "Entity not available" fallback: if the active entity is untracked,
  the bar stays visible (so the user can still navigate back into a
  tracked ancestor) and only the body shows the fallback message.

### `breadcrumb/BreadcrumbBar.tsx` (new)

Layout:

```
┌─────────────────────────────────────────────────────────────┐
│ [◧]  A  ›  B  ›  C  ›  D                                     │
│  ↑      (past)  ↑active (future gray)                        │
└─────────────────────────────────────────────────────────────┘
```

- Leftmost: history-root icon (e.g. ⟲ / ◧ / "…" glyph). Renders as a
  base-ui `Menu.Root` trigger. Hidden/disabled when
  `historyRoot.kind === "namespace"` and no deeper ancestry exists —
  or we can always show it and give a nicer empty-state menu. Go with
  always-show for layout stability; menu says "Opened from the
  namespace" when there's no content.
- Middle: chain entries, separated by `›`. Each entry is a custom
  `<BreadcrumbEntry chainIndex={i} entityId={...} />` component.
  - Past entries: normal color.
  - Active entry: `color: var(--text-primary)`, `font-weight: 600`,
    background ring.
  - Future entries (index > activeIndex): `color: var(--text-muted)`.
- Overflow: horizontal scroll with no scrollbar chrome + a subtle
  gradient fade at right edge when content exceeds width. Post-MVP we
  can collapse middle entries into `…` if the chain gets really long;
  for step 6, scroll is enough.

Label rendering per entry: `<Render entity={id} as={InlineLink} />`
inside a `Suspense` (option (b) from the Q&A). This reuses the
dispatch-resolved compact display for labels. Each entry gets its own
`<Suspense fallback={shortId(id)}>` so one slow-loading ancestor
doesn't freeze the whole bar.

### `breadcrumb/BreadcrumbEntry.tsx` (new)

Each entry composes the four interaction hooks (same ones
`EntityLink` uses) but with a custom click handler:

- **Click** → `activateChainEntry(panelId, chainIndex)`.
  *Explicitly not* `useEntityClick`; that would call `followLink`
  and cause a chain mutation.
- **Hover** → `useEntityHoverCard(entity)` → normal HoverPreview.
- **Right-click** → menu item "Open in new panel" that calls
  `restoreIntoNewPanel(computeOpenNewFromChainEntry(...))`.
- **Drag** → custom `useDraggableChainEntry(panelId, chainIndex)` that
  reads chain state from `PanelContext` and packs the resolved
  `DragPayload`.

Visual state comes from `isActive` / `isFuture` props the bar
computes.

### `breadcrumb/HistoryRootMenu.tsx` (new)

Opened by clicking the history-root icon. Implemented with base-ui
`Menu.Root`/`Menu.Portal`/`Menu.Popup`. Inside:

- For each segment (from deepest to shallowest), a group header
  (e.g. "Opened from panel — via drag" or just a thin divider) and
  then a `<HistoryRootEntry>` per entity in that segment's prefix.
- Each entry uses the same interaction kit as `BreadcrumbEntry` but
  with:
  - **Click** → `restoreIntoNewPanel(computeOpenNewFromHistoryEntry(...))`.
    Closes the menu.
  - **Hover** → HoverPreview.
  - **Drag** → `useDraggableHistoryEntry(...)`.
  - Right-click: **omit for step 6** (the entry lives inside an
    already-open menu; a second context menu layered on top is
    confusing).

Styling uses `theme/context-menu.css` tokens (same palette as the
right-click menu, different class name so we can diverge later).

### `interactions/useEntityClick.ts` — panel-context branch

- Panel context present → `followLink({ target, trail, sourcePanelId,
  intent: { kind: "replace-in-source" } })`.
- No panel context → `restoreIntoNewPanel(computeSnapshotFromTrail(
  trail, entity, { kind: "namespace" }))`.

### `interactions/useEntityContextMenu.tsx` — panel-context branch

"Open in new panel" item:

- Panel context present → `followLink({ ..., intent: { kind: "open-new" } })`
  (trail is captured, history root is derived at the shell).
- No panel context → `restoreIntoNewPanel(computeSnapshotFromTrail(...))`.

### `interactions/useDraggableEntity.ts` — panel-context branch

- Panel context present → pack a `LinkDragPayload` (target id, trail,
  sourcePanelId). `followLink` resolves at drop time.
- No panel context → pack a `SnapshotDragPayload` built via
  `computeSnapshotFromTrail`. Drop handler routes to
  `restoreIntoNewPanel`.

### `interactions/drag.ts` — new payload shape

Replace the old `{entityIdString, trail, sourcePanelId}` payload with
the `{newPanelState, entityIdString, sourcePanelId?}` shape. Update
`setDragData` / `parseDragData` accordingly. MIME unchanged.

### `DockviewLayout.tsx` — discriminated drop handler

See "Drag payload redesign" above for the dispatch. In short, drops
branch on `payload.kind`: `"link"` → `followLink(open-new, position)`;
`"snapshot"` → `restoreIntoNewPanel(state, position)`.

The `navigate` prop on this component is replaced with the full
`WorkstationApi` (or the two method refs it needs).

### `WorkstationShell.tsx` — implements the three-method API

Responsibilities it already owned (dockview api, panel-id counter,
layout persistence, hover-close registry) carry forward. Additions:

- `followLink(event)`:
  - Precondition: `sourcePanelId` refers to an existing entity panel.
    (Interaction hooks enforce this by routing non-panel origins
    through `restoreIntoNewPanel` instead.) If the precondition is
    violated anyway — e.g. the source panel was closed between
    drag-start and drop — log a warning and no-op. No silent panel
    spawn.
  - `replace-in-source`: read source panel's params, compute next
    state via `history.computeNextChainState(...)`, `updateParameters`.
  - `open-new`: read source state, compute state via
    `history.computeOpenNewFromLink(...)`, call `restoreIntoNewPanel`.
- `activateChainEntry(panelId, index)`:
  - Look up panel params, shift `activeIndex`, `updateParameters`.
  - No-op if `index === activeIndex`.
- `restoreIntoNewPanel(state, position?)`:
  - Generate `entity-<n>` id, `api.addPanel({... params: state ...})`.

`WorkstationContext` now carries `{ followLink, activateChainEntry,
restoreIntoNewPanel }` instead of `{ navigate }`. Existing `navigate` export and
`NavigateEvent`/`NavigateHandler` types are removed; keep
`DropPosition` + `Breadcrumb*` + `FollowLinkEvent` as the new public
surface.

### `panels/NamespacePanel.tsx` — no special code

The namespace panel keeps using `<EntityLink as="block">` rows. All
the routing happens one level deeper: the interaction hooks inside
`EntityLink` see a null `PanelContext` and take the
`restoreIntoNewPanel` / `SnapshotDragPayload` branch (see "Non-panel
origins" above). The namespace panel itself has no knowledge of the
navigation machinery — it just renders pins.

### `sidebar/usePanelLayout.ts`, `sidebar/OpenPanelsSidebar.tsx`

Sidebar row surfaces the *active* entity in each panel. Today
`usePanelLayout` reads `params.entityIdString`; update it to read
`params.chain[params.activeIndex]`. Row click still focuses; X still
closes. No breadcrumb / history UI in the sidebar (out of scope).

## Persistence

- `LAYOUT_STORAGE_VERSION` bumps from 2 → 3 (params shape change).
  Version mismatch triggers the default layout; users lose their
  saved layout once across this bump.
- `collectEntityIdStringsFromLayout` → `collectEntityIdStringsFromLayout`
  (keep the name, update body): walks each entity panel's `chain` and
  each `historyRoot` prefix chain, collecting every id referenced. All
  of these are awaited through `tryRetrieveById` so breadcrumb-bar
  labels can render without a cold miss on restore.
  - Rationale: we don't *need* historyRoot labels to load immediately
    (the menu is closed by default), but the cost is small and avoids
    suspense flashes when the user first opens the menu.

## Demo — `DemoListNode` linked list

Purpose: a 20-deep chain to exercise the breadcrumb mechanics in the
dev demo.

### Entity

```ts
const _DemoListNode = bicodec.entity(defineType(NS, "DemoListNode"), {
  label: bicodec.string(),
  // null = end of list. Use bicodec.lazy to support self-reference.
  next: bicodec.nullable(bicodec.lazy(() => bicodec.entityRef(DemoListNode))),
});
```

### Server seeding

Create 20 nodes labeled `"Node 20"` → `"Node 1"` in reverse so each
can point to the next one. Pin `demo/chain-head` → node 1. Keep
existing DemoNote/Section/Archive pins.

### Client handlers

- **`DemoListNodeFullPanel`** — shows `label` prominently, then an
  `<EntityLink as="block">` to `next` if present, else "end of list".
  Calls `setPanelTitle(label)`.
- **`DemoListNodeBlockLink`** — compact pill showing the label (same
  style idea as `DemoNoteBlockLink`).
- **`DemoListNodeHoverPreview`** — label + a nested
  `<EntityLink as="block">` to `next`. The nested link is the key
  UX-testing piece: you can hover a chain-node preview and then hover
  its `next` to build a deep trail of nested previews, then drag from
  the deepest one to see the branch semantics (new panel's chain =
  trail tail, historyRoot = source state at branch).

Walking `demo/chain-head` downward exercises the "click-replace-in-
source appends to chain" case for 20 steps; clicking the history-root
icon after a sidebar-branch exercises the deep history restore path.

## File changes summary

```
fxtr-workstation-core/src/
├── breadcrumb/
│   ├── types.ts                     (unchanged; trail shape reused)
│   ├── BreadcrumbSourceContext.tsx  (unchanged)
│   ├── history.ts                   NEW — pure transformations
│   ├── flatten.ts                   NEW — HistoryRoot → FlatHistoryEntry[]
│   ├── BreadcrumbBar.tsx            NEW
│   ├── BreadcrumbEntry.tsx          NEW
│   ├── HistoryRootMenu.tsx          NEW
│   └── HistoryRootEntry.tsx         NEW
├── workstation/
│   ├── navigate.ts                  REWRITE — new WorkstationApi shape
│   ├── WorkstationContext.tsx       EDIT   — carries new api
│   └── PanelContext.tsx             EDIT   — adds chain/active/root
├── interactions/
│   ├── drag.ts                      EDIT   — discriminated payload shape
│   ├── useDraggableEntity.ts        EDIT   — PanelContext branch (link vs snapshot)
│   ├── useEntityClick.ts            EDIT   — PanelContext branch (followLink vs restore)
│   ├── useEntityContextMenu.tsx     EDIT   — PanelContext branch (same)
│   ├── useDraggableChainEntry.ts    NEW
│   └── useDraggableHistoryEntry.ts  NEW
├── EntityPanelWrapper.tsx           EDIT   — new params, renders bar
├── DockviewLayout.tsx               EDIT   — discriminated drop handler
├── WorkstationShell.tsx             EDIT   — implements new api, v3 persist
├── panels/NamespacePanel.tsx        (no change — EntityLink branching handles it)
├── sidebar/usePanelLayout.ts        EDIT   — reads chain[activeIndex]
└── index.ts                         EDIT   — exports

fxtr-workstation-theme/styles/
└── breadcrumb.css                   NEW (+ import in all.css)

example-dev-workstation/src/
├── entities.ts                      EDIT   — + DemoListNode
├── server.ts                        EDIT   — seed 20-node chain + pin
└── client/handlers.tsx              EDIT   — + DemoListNode handlers

CODE_OVERVIEW.md                     EDIT   — reflect step 6 shape
plans/phase0/overview.md             EDIT   — check off step 6
```

## Implementation order

Roughly dependency order; each check is a natural commit boundary:

- [x] `breadcrumb/history.ts` — pure transformations (`computeNextChainState`,
      `computeOpenNewFromLink`, `computeOpenNewFromChainEntry`,
      `computeOpenNewFromHistoryEntry`, `computeSnapshotFromTrail`).
      Tiny unit tests in a colocated `history.test.ts` (first real
      vitest of the workstation; set up minimal config). Covers each
      branch of the `replace-in-source` rule, the unflatten-preserving
      history-root restore, and the non-panel snapshot case.
- [x] `breadcrumb/flatten.ts` — `flattenHistoryRoot(root) → FlatHistoryEntry[]`.
      Colocated test for the flatten-then-restore round-trip.
- [x] `workstation/navigate.ts` — new `WorkstationApi` + `FollowLinkEvent`
      types; remove `NavigateEvent`/`NavigateHandler`.
- [x] `workstation/WorkstationContext.tsx` — provides new api shape.
- [x] `workstation/PanelContext.tsx` — extend with chain + active +
      historyRoot.
- [x] `interactions/drag.ts` — payload shape swap + helpers.
- [x] `interactions/useDraggableEntity.ts` — PanelContext branch:
      `LinkDragPayload` when present, `SnapshotDragPayload` (via
      `computeSnapshotFromTrail`) when absent.
- [x] `interactions/useEntityClick.ts` — PanelContext branch
      (`followLink(replace)` vs `restoreIntoNewPanel`).
- [x] `interactions/useEntityContextMenu.tsx` — same branch pattern for
      "Open in new panel".
- [x] `WorkstationShell.tsx` — implements `followLink` /
      `activateChainEntry` / `restoreIntoNewPanel`; swaps to new params shape;
      bumps `LAYOUT_STORAGE_VERSION` to 3; updates
      `collectEntityIdStringsFromLayout` to walk chain + historyRoot.
- [x] `DockviewLayout.tsx` — drop handler discriminates
      `LinkDragPayload` (→ `followLink(open-new, position)`) vs
      `SnapshotDragPayload` (→ `restoreIntoNewPanel(state, position)`).
- [x] `EntityPanelWrapper.tsx` — consumes new params; hydrates active
      entity; provides extended PanelContext; renders
      `<BreadcrumbBar />` above FullPanel render.
- [x] `panels/NamespacePanel.tsx` — verify no changes needed (the
      interaction hooks inside `EntityLink` handle the no-panel-context
      case now). If anything here did drift (e.g. a stale
      `sourcePanelId` attribution), fix at this step.
- [x] `sidebar/usePanelLayout.ts` — read `chain[activeIndex]`.
- [x] Update `CODE_OVERVIEW.md` (interim — through the plumbing
      layer, before breadcrumb UI).
- [x] `theme/styles/breadcrumb.css` — `.fw-breadcrumb-bar`,
      `.fw-breadcrumb-entry`, `.fw-breadcrumb-entry--active`,
      `.fw-breadcrumb-entry--future`, `.fw-history-root-button`,
      `.fw-history-root-menu`, `.fw-history-root-entry`,
      `.fw-history-root-segment-separator`. Imported from `all.css`.
      Subpath export in `package.json`.
- [x] `breadcrumb/BreadcrumbEntry.tsx` — single entry with
      click/hover/right-click/drag. New hook
      `useDraggableChainEntry`.
- [x] `breadcrumb/BreadcrumbBar.tsx` — chain row + overflow handling.
- [x] `breadcrumb/HistoryRootEntry.tsx` + `useDraggableHistoryEntry`.
- [x] `breadcrumb/HistoryRootMenu.tsx` — base-ui Menu, segmented
      entries.
- [x] Wire the menu's trigger icon into `BreadcrumbBar`.
- [x] `index.ts` export updates.
- [x] Demo: `DemoListNode` entity + codec; server seeding; client
      handlers (FullPanel / BlockLink / HoverPreview with nested
      link).
- [x] `pnpm check` + `pnpm lint` clean.
- [ ] Manual smoke (pending user):
      - Walk chain-head downward 20 steps via clicks → single panel,
        chain grows to 20.
      - Click older breadcrumb entry → active shifts, FullPanel
        remounts, future stays.
      - Click a link beyond the current next → future gets truncated
        and the link appended.
      - Right-click a ref → new panel, history root = current chain
        prefix.
      - Drag a ref from a double-nested hover → new panel chain has
        the hover tail, history root = source chain prefix.
      - Click the history-root icon → menu shows all ancestor
        segments; clicking an entry deep in a segment opens a new
        panel with the correct unflattened shape.
      - Drag a breadcrumb entry → new panel with rewound chain, same
        historyRoot.
      - Reload → layout restored with all chains + history roots intact.
- [x] Update `CODE_OVERVIEW.md` with the final step-6 shape.
- [x] Check off step 6 in `overview.md`.

## Open questions / risks to flag

1. **Unfied or split `navigate`?** The plan splits into three methods
   (`followLink` / `activateChainEntry` / `restoreIntoNewPanel`). Alternative:
   keep a single `navigate(event)` with a discriminated-union event
   type. Split feels more obvious-by-construction — each call site's
   capability is visible in the method name — but the call sites are
   few enough that union-dispatch would also work. Revisit once the
   first draft is in.
2. **Breadcrumb labels via `InlineLink` load cost.** Rendering 20
   entries × a Suspense-bounded `<Render as={InlineLink}>` means 20
   entity loads just to paint the bar (all already-cached entities
   will be instant; worst case is right after restore before
   `tryRetrieveById` finishes). Acceptable for MVP; if it becomes a
   real problem we can add a `shortId` fallback for non-loaded
   ancestors (the Suspense fallback already does this).
3. **Self-referential bicodec.** `bicodec.lazy` exists, but if the
   `DemoListNode` self-ref hits any edge case in the codec graph
   (codec registration order, id stability), we may need to fall
   back to modeling the chain as two alternating types. Flagged here
   so implementation doesn't spin on it.
4. **Drop-into-same-panel.** Currently an `EntityLink` dragged and
   dropped back onto the same panel would create a new panel next to
   it (payload carries `newPanelState`, dockview routes to drop
   position). That's the same behavior as today — noted so we don't
   expect drop-to-replace semantics.
5. **History-root icon empty-state.** When `historyRoot.kind ===
   "namespace"` and no prior branches exist, the flat list is empty.
   We keep the button visible for layout stability; the open menu
   just shows "Opened from the namespace." — consider disabling the
   button instead if it feels cluttered.
6. **Activation via breadcrumb click — any remount cost concern?** The
   inner `<Render key={activeIdString}>` forces FullPanel remount on
   activeIndex change, which is the intended behavior (entity changes
   → fresh renderer instance) but means clicking back/forward loses
   any transient per-renderer state. Matches current
   `replace-in-source` behavior; only surfaces as a new concern
   because now there's a back/forward UI to exercise it.
7. **Sidebar tooltip + row label on active-index change.**
   `usePanelLayout` currently builds `SidebarPanel.entity` once per
   params-change cycle. Confirm it rebuilds when `activeIndex`
   changes (it should, since `updateParameters` triggers
   `onDidLayoutChange`). Note to verify during implementation.
8. **History-root menu during drag.** If the user starts a drag from
   a history-root entry, the popover typically closes on pointer-down.
   base-ui `Menu` should let the drag continue even after the popup
   unmounts because `dataTransfer` is the browser's own state, but
   confirm. Mitigation: explicitly keep the menu open on drag start
   until dragend, if needed.
- [x] Update `CODE_OVERVIEW.md` (final — after step 6 lands).

## Implementation notes / things worth revisiting

10. **Vitest config / workstation-core devDep.** Existing v2 packages
    find `vitest` by walking up from `v2/ts/packages/*/tests/` into
    `v2/ts/node_modules/vitest`. Workstation-core's tests live at a
    different tree root (`projects/fxtr_workstation/ts/packages/...`)
    and that walk-up doesn't cross into `v2/ts/`, so tsc couldn't
    resolve the type import. Fix: added `vitest` to
    workstation-core's `devDependencies` so pnpm links it locally,
    and extended `vitest.config.ts` `include` to also match
    `../../projects/fxtr_workstation/ts/packages/*/tests/**`.
11. **`EntityPanelParams` moved.** Was local in `EntityPanelWrapper.tsx`
    as `{ entityIdString }`; now defined in `breadcrumb/types.ts` as
    `{ chain, activeIndex, historyRoot }` and re-exported from the
    wrapper for compatibility. This makes `history.ts`'s imports
    cleaner (types.ts has no React deps).
12. **Self-referential `DemoListNode` codec.** Used the `TreeNode`
    pattern from the existing bicodec tests: explicit
    `DemoListNodeFields` interface, then
    `bicodec.entity(..., { next: bicodec.nullable(bicodec.lazy(() =>
    bicodec.entityRef(DemoListNode))) })`. `lazy` defers resolution
    so the forward reference to `DemoListNode` (declared after
    `_DemoListNode`) is resolved at runtime. Server seeds the chain
    from tail to head so each predecessor can reference the stored
    id of its successor.
13. **`BreadcrumbBar` is always visible, even when
    `historyRoot.kind === "namespace"`.** The history-root button
    stays for layout stability; the menu itself shows "Opened from
    the namespace." rather than rendering nothing. If this feels
    cluttered in practice, disable the button instead.
14. **`DraggableEntityHandle` unchanged.** It calls
    `useDraggableEntity` which now branches internally, so the
    handle automatically does the right thing (link payload inside a
    panel; snapshot payload outside). No caller-side changes needed.
15. **Trigger element type cast in `HistoryRootMenu`.** base-ui's
    `Menu.Trigger` `render` prop is typed as a ReactElement; we
    accept a `ReactNode` in the public API for flexibility and cast
    at the boundary. If callers pass a non-element (string, array),
    it will surface as a runtime issue from base-ui; fine for now,
    documented here as a sharp edge.
16. **Panel title does not yet include chain-step info.** Dockview
    tab title still shows only the active entity's title (via the
    existing `setPanelTitle` contract). The breadcrumb bar inside
    the panel already shows the chain, so this is fine — but if we
    later want a visual "3 of 7" hint on the tab, that'd require
    extending the title rendering.
17. **`EntityPanelWrapper` renders `BreadcrumbBar` even when the
    active entity is untracked.** Intentional — the user can still
    navigate back into a tracked ancestor via the bar. The body
    shows the "Entity not available" message below it.
