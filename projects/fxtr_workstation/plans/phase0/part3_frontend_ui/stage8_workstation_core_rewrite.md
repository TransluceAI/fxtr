# Stage 8 — `fxtr-workstation-core` rewrite onto `@fxtr-ui/*`

Port the existing dockview shell, breadcrumb / history machinery,
sidebar, and panel system off `@fxtr/core` + `@fxtr/react` and onto
the new `@fxtr-ui/*` foundation. Each panel owns its own workstation
session (created eagerly on open, terminated on close, attached on
reload). The deliverable is a Vite demo at visual parity with the
existing `example-dev-workstation`, but now backed by a Python
workstation server that exposes its root entities + a couple of
services via peer-RPC self-loopback.

This stage **does not** integrate the HTTP connector (stage 5 still
deferred). Sessions ride entirely on the workstation `/client`
WebSocket protocol.

## Status

Chunk progress:

- [x] Chunk 1 — Package scaffold, dispatch keys, default fallbacks
- [x] Chunk 2 — Navigation surface, `RefLink`, interaction hooks
- [x] Chunk 3 — Per-panel session lifecycle + `PanelWrapper`
- [x] Chunk 4 — `WorkstationShell` + navigation implementation
- [x] Chunk 5 — Breadcrumb + history-root menu
- [x] Chunk 6 — Roots panel (registry browser)
- [x] Chunk 7 — Open-panels sidebar
- [x] Chunk 8 — Layout persistence v4
- [x] Chunk 9 — Demo backend + Vite dev client (initial; expect iteration during boot-and-fix)
- [skipped] Chunk 10 — Tests. Skipped intentionally — the workstation frontend is iterating fast and interactive testing has been more valuable than locked-in unit/component tests would be at this stage. Revisit once the surface has stabilized.
- [x] Update `CODE_OVERVIEW.md` (+ new `packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md`)

## Design decisions

These are the load-bearing choices going in. Open questions live at
the bottom of this file.

- **One session per ref panel; no session for info panels.**
  Ref panels (`kind: "ref"`) own a session that persists chain /
  active / historyRoot / per-renderer storage. Workstation-level
  info panels (`kind: "roots"`, plus future settings / debug
  views) have no per-panel state to persist and skip the session
  machinery entirely — their params carry only the `kind`
  discriminator, they don't go through `usePanelSession`, they
  don't register with `PanelSessionRegistry`, and the shell adds
  them with a single synchronous `dockview.addPanel` call.
  Session **creation is shell-owned** for the ref case: shell
  mints the session via `connector.createSession(...)`, writes
  the seed chain state, detaches its own subscription, then
  writes `sessionId` onto the dockview params; the panel
  observes the change and attaches. Close → `session.terminate()`.
  Page reload → `connector.attachSession(persistedId)` per ref
  panel; if the subscription's `subscribe_failed` fires, the
  panel's lifecycle flips to `"unavailable"` and `PanelWrapper`
  renders the "session no longer available" body.
- **Single shared jotai store.** All sessions (the dispatcher
  session and every panel session) share `connector.store`. The
  `<FxtrSessionProvider>` wrap is still used per-panel so
  `useSession()` resolves to the right session, but it doesn't
  isolate the store. Per-panel persisted state isolation comes from
  the fact that each session's atoms are distinct *instances*.
  In-memory atom families keyed on session ID can layer on later if
  we need them.
- **Dispatch keys distinguish interactive from non-interactive only
  via their consumer `Props` type.** The dispatch system itself is
  unchanged from stage 7. The `FullPanel` key declares
  `ownStorage: FxtrSessionAtom<StateValue>` in its props (the
  consumer — `PanelWrapper` — passes a sub-atom of the panel's
  session userState). The other three keys (`BlockLink` /
  `InlineLink` / `HoverPreview`) have no atom prop; per-(stub, key)
  state lives in the dispatcher session via `useDispatchSharedAtom`
  as today.
- **Refs are stubs.** The new shell renders `EntityRefStub` or
  `ServiceRefStub` interchangeably wherever it currently renders an
  entity-typed ref. Chain, history-root, drag payloads, panel
  params, breadcrumb labels, sidebar tooltips, hover cards — all
  generalize over a `StubRef = { kind: "entity" | "service"; shortId
  }`. Service-typed dispatch entries fall back to a generic
  "service" component for each of the four keys (shipped by
  `@fxtr-ui/workstation`). Stage 8 does not include real service
  renderers; that's stage 9 territory.
- **Renames.** `EntityLink` → `RefLink`. `EntityPanelWrapper` →
  `PanelWrapper`. `NamespacePanel` → `RootsPanel`. The interaction
  hooks (`useEntityClick` etc.) drop the `Entity` prefix. The
  `historyRoot` recursive type's `{kind: "namespace"}` becomes
  `{kind: "roots"}`. The chain is `StubRef[]`, not `string[]`.
- **No `NamespaceContext`.** The old shell read entities by id via
  `useNamespace()` calling an `FxtrReadNamespace`. The new shell has
  no analogous concept — refs come from the connector's intern maps
  (`connector.entityRef(shortId)` / `connector.serviceRef(shortId)`)
  and from wire-decoded stubs in session state. We thread the
  connector wherever the old code threaded the namespace.
- **Per-mount state shape is the renderer's concern.** Following
  design note 05 OQ-iv option (b): `PanelWrapper` passes an
  `ownStorage` sub-atom that's typed `FxtrSessionAtom<StateValue>`
  (loosely typed). The renderer uses `atomWithDefault` to project
  it to whatever shape it wants and handles the "first render, no
  state yet" case explicitly. No per-renderer `initialState()`
  hook.
- **Demo backend = peer-RPC self-loopback.** No new
  `ExternalEntityConfig`. The Python demo entry boots a
  `WorkstationServer`, opens a peer-RPC connection to its own
  `/rpc`, and uses `expose_entity` / `expose_service` to register
  demo content under stable external names. This matches the
  pattern already established in
  [`_demo_workstation.py`](../../../frontend_lib/packages/fxtr-ui-connector-workstation/tests/_demo_workstation.py).

## New package layout

The new code lives under `frontend_lib/packages/fxtr-ui-workstation/`
alongside the other `@fxtr-ui/*` packages. The old
`projects/fxtr_workstation/ts/packages/fxtr-workstation-core/` and its
two example packages stay in place during the rewrite — they're
deleted at the end once parity is reached.

```
frontend_lib/packages/fxtr-ui-workstation/
├── package.json                 # deps: @fxtr-ui/* (workspace), dockview, @base-ui/react; peer: react
├── tsconfig.json
└── src/
    ├── index.ts
    ├── dispatch/
    │   ├── keys.ts              # FullPanel (with ownStorage), BlockLink, InlineLink, HoverPreview
    │   ├── registry.ts          # createWorkstationDispatchRegistry — thin wrapper installing defaults
    │   └── default_fallbacks/
    │       ├── EntityFullPanelFallback.tsx
    │       ├── EntityBlockLinkFallback.tsx
    │       ├── EntityInlineLinkFallback.tsx
    │       ├── EntityHoverPreviewFallback.tsx
    │       ├── ServiceFullPanelFallback.tsx
    │       ├── ServiceBlockLinkFallback.tsx
    │       ├── ServiceInlineLinkFallback.tsx
    │       ├── ServiceHoverPreviewFallback.tsx
    │       └── PanelErrorBoundary.tsx
    ├── reflink/
    │   ├── RefLink.tsx          # public component, dispatches on either ref kind
    │   ├── DraggableRefHandle.tsx
    │   ├── useRefClick.ts
    │   ├── useRefContextMenu.tsx
    │   ├── useRefHoverCard.tsx
    │   ├── useDraggableRef.ts
    │   ├── useDraggableChainEntry.ts
    │   ├── useDraggableHistoryEntry.ts
    │   ├── HoverContext.tsx
    │   └── drag.ts              # discriminated DragPayload + MIME constants
    ├── shell/
    │   ├── WorkstationShell.tsx
    │   ├── WorkstationContext.tsx
    │   ├── WorkstationApi.ts    # interface types only
    │   ├── DockviewLayout.tsx
    │   └── ConnectorContext.tsx # connector prop drilling alternative — TBD if we need it
    ├── panel/
    │   ├── PanelWrapper.tsx
    │   ├── PanelContext.tsx
    │   ├── PanelSessionLifecycle.ts  # pending → real → terminated state machine helpers
    │   └── SessionUnavailable.tsx
    ├── breadcrumb/
    │   ├── types.ts             # StubRef, ChainEntry, HistoryRoot, EntityPanelParams (→ RefPanelParams)
    │   ├── BreadcrumbBar.tsx
    │   ├── BreadcrumbEntry.tsx
    │   ├── BreadcrumbSourceContext.tsx
    │   ├── HistoryRootMenu.tsx
    │   ├── HistoryRootEntry.tsx
    │   ├── flatten.ts
    │   └── history.ts           # pure transforms: computeNextChainState, etc. — stub-generalized
    ├── panels/
    │   ├── RootsPanel.tsx
    │   └── rootsPanelComponent.ts
    ├── sidebar/
    │   ├── OpenPanelsSidebar.tsx
    │   ├── ResizableSidebar.tsx
    │   ├── usePanelLayout.ts
    │   └── SidebarTooltip.tsx
    ├── persistence/
    │   ├── layout_v4.ts         # serialize / deserialize + version migration
    │   └── useLayoutPersistence.ts
    └── components/
        └── JsonTree.tsx
```

Theme CSS lives **inside** the new package under
`src/styles/` (copied from the old `@fxtr-workstation/theme`
package). The new package has no cross-package CSS dep; consumers
do `import "@fxtr-ui/workstation/styles/all.css"`. The
`fw-`-prefixed class names carry over verbatim. The old
`@fxtr-workstation/theme` package stays in place until the old
core is retired alongside it.

## Key types and surfaces

Type sketches — not full signatures, just the contract shape.

**Stub generalization.**

```ts
type StubKind = "entity" | "service";
interface StubRef {
  readonly kind: StubKind;
  readonly shortId: string;
}
// Plus: convert to/from EntityRefStub | ServiceRefStub via connector intern maps.
```

The new shell stores `StubRef` (just the discriminator + short ID)
in JSON-persistable params. The corresponding `EntityRefStub` /
`ServiceRefStub` is resolved through the connector at use time.

**Dispatch keys.**

```ts
// Interactive
declareDispatchKey<{ ownStorage: FxtrSessionAtom<unknown> }>(UUID_FULL_PANEL, "FullPanel")

// Non-interactive
declareDispatchKey<{ linkText?: string }>(UUID_BLOCK_LINK,  "BlockLink")
declareDispatchKey<{ linkText?: string }>(UUID_INLINE_LINK, "InlineLink")
declareDispatchKey<NoProps>(UUID_HOVER_PREVIEW, "HoverPreview")
```

The dispatch system doesn't know which are interactive — that's a
naming convention enforced only by the consumer who chooses what to
pass. `ownStorage` is typed `FxtrSessionAtom<unknown>` because
`PanelWrapper` (the consumer) doesn't know what shape its renderer
will want; the renderer narrows on its end (typically with
`atomWithDefault` + `useSubAtom`).

**Panel params.**

```ts
type PanelKind = "ref" | "roots";

// Persisted in dockview params
type PanelParams =
  | RefPanelParams       // a panel rendering a chain of stub refs
  | RootsPanelParams;    // the registry-browser panel

interface RefPanelParams {
  kind: "ref";
  sessionId: string | null;     // null while session is being created
  // Chain / activeIndex / historyRoot are stored in the session's
  // userState, NOT here. The params just carry enough to attach.
}

interface RootsPanelParams {
  kind: "roots";
  sessionId: string | null;     // for symmetry; doesn't store much
}
```

The user-facing chain state in a `RefPanel`'s session looks like:

```ts
interface RefPanelUserState {
  chain: StubRef[];                // [{kind,shortId}, ...]
  activeIndex: number;
  historyRoot: HistoryRoot;
  // Plus: a slot for the dispatched FullPanel's own ownStorage —
  // a key inside this object that PanelWrapper reserves and slices
  // via useSubAtom + atomWithDefault.
  ownStorage?: StateValue;
}
```

**`HistoryRoot`** generalizes today's `{kind: "namespace"} | {kind:
"branched"; prefix; parent}`:

```ts
type HistoryRoot =
  | { kind: "roots" }
  | { kind: "branched"; prefix: StubRef[]; parent: HistoryRoot };
```

**`WorkstationApi`** keeps today's three-method shape but
stub-generalized:

```ts
interface WorkstationApi {
  followLink(event: FollowLinkEvent): void;            // panel-context navigations
  activateChainEntry(panelId: string, idx: number): void;
  restoreIntoNewPanel(params: RefPanelParams, pos?: DropPosition): void;
}
```

`FollowLinkEvent` carries `target: StubRef`, `trail: Trail`,
`intent: "replace" | "new"`, etc., generalized from today's
entity-only shape.

**Drag payload** stays discriminated:

```ts
type DragPayload =
  | { kind: "link"; target: StubRef; trail: Trail; sourcePanelId?: string }
  | { kind: "snapshot"; newPanelState: RefPanelParams };
```

## Per-panel session lifecycle

A panel transitions through:

1. **Pending.** Params are `{sessionId: null}`. `PanelWrapper`
   renders a small "creating session..." body and waits. Session
   creation is the **shell's** responsibility: when the shell
   opens a new ref panel (via `restoreIntoNewPanel`,
   `followLink({intent: "open-new"})`, etc.) it
   (a) adds the dockview panel with `sessionId: null`,
   (b) `connector.createSession(...)` and writes the seed chain
   state into the new session's `userState`,
   (c) `dockviewApi.updateParameters({sessionId: newId})` to
   notify the panel. The panel re-renders with the real id and
   transitions to `live`. The shell can detach its own
   subscription after the seed commit since the panel will
   re-attach for itself.
2. **Live.** Params are `{sessionId: <real>}`. `PanelWrapper` does
   `connector.attachSession(sessionId)`, wraps in
   `<FxtrSessionProvider>`, and renders the appropriate body
   (`RefPanelBody` for `kind: "ref"`, `RootsPanelBody` for `kind:
   "roots"`).
3. **Session-missing.** The session subscription's
   `subscribe_failed` close arrives. The abstract `Session`'s
   `SessionLifecycleState` union gains a new terminal state
   (tentative name: `"unavailable"`) distinct from `"closed"`, so
   "session no longer exists on the server" is observable on the
   lifecycle atom alone. `WorkstationSession`'s FSM emits
   `unavailable` when its initial register call closes with
   `subscribe_failed`; `closed` stays reserved for clean
   `state_update.closed=true` and post-`terminate()` paths.
   `PanelWrapper` renders `<SessionUnavailable />` with a "reopen"
   button that mints a fresh session.
4. **Closed.** Panel is removed from dockview. Pre-removal hook
   calls `session.terminate()`. Layout persistence drops the entry.

Session ID is the only durable state in `params`. Everything else
lives in session userState.

## Layout persistence v4

`localStorage["fxtr-workstation-layout"]` v4 payload:

```ts
{
  version: 4,
  dispatcherSessionId: string,       // single dispatcher session for the app
  dockview: <dockview-json>,         // each panel's params carries sessionId
  sidebar: { collapsed: boolean; width: number },
}
```

On boot the shell:

1. Reads + version-checks the payload. Version mismatch → drop +
   build default layout (one roots panel).
2. Mints the dispatcher session up front. If
   `dispatcherSessionId` is persisted, the shell `attachSession`s
   to it; otherwise it `createSession`s and remembers the new ID.
   This is a synchronous `attachSession` call — no awaiting; if
   the session turns out to be missing, the failure surfaces via
   lifecycle (`unavailable`) on first use, and the dispatcher
   provider can fall back to a fresh create. We don't block boot
   on the network.
3. Calls `api.fromJSON(dockview)` to install the layout.
4. Each panel resolves its own session asynchronously through
   `PanelWrapper` — pending / live / unavailable transitions are
   per-panel, with no shell-level synchronization. The shell
   doesn't peek at panel subscriptions.
5. Persists changes on dockview events + sidebar state changes
   (200ms debounced, same as today).

Default layout (no persisted state): one roots panel.

## Migration map

Files under `projects/fxtr_workstation/ts/packages/fxtr-workstation-core/src/`
classified:

| Old file | Disposition | Notes |
|---|---|---|
| `keys.ts` | Rewrite (done) | Four keys, `FullPanel` carries `ownStorage`, hardcoded UUIDs; `LinkProps` flat shape (`linkText?`) |
| `shortId.ts` | Drop (done) | `shortId` lives on stubs directly |
| `registry.ts` | Rewrite (done) | `createWorkstationDispatchRegistry({entity,service})` wires defaults via `staticEntity/ServiceTypeDispatch` |
| `EntityLink.tsx` | Rewrite as `RefLink.tsx` (done) | Accepts `stub: EntityRefStub | ServiceRefStub`; routes through `RenderDispatch as={BlockLink|InlineLink}` |
| `DockviewLayout.tsx` | Copy + adapt (done) | Registers `PanelWrapper` for `REF_PANEL_COMPONENT`; drag-MIME is now `application/x-fxtr-ref`; routes drops via the new payload shape |
| `EntityPanelWrapper.tsx` | Rewrite as `PanelWrapper.tsx` (done) | Attach-only via `usePanelSession` (shell owns creation); pending/live/unavailable branches; FullPanel dispatch with `ownStorage` sub-atom; registers its live session with `PanelSessionRegistry` |
| `WorkstationShell.tsx` | Rewrite (done) | Mints dispatcher session on mount; waits for `connected` before exposing the tree; implements `WorkstationApi` against per-panel sessions via `PanelSessionRegistry`; open-new-panel flow seeds chain state through a shell-owned subscription that detaches once the seed commits |
| `breadcrumb/types.ts` | Rewrite (done) | `Breadcrumb` carries `StubRef`; `HistoryRoot` `{kind:"roots"}` base case; new `RefPanelChainState` shape |
| `breadcrumb/BreadcrumbSourceContext.tsx` | Copy (done) | Generic over the new `BreadcrumbSource` |
| `breadcrumb/history.ts` | Rewrite (done) | Pure transforms over `RefPanelChainState`; `StubRef` equality by kind+shortId |
| `breadcrumb/flatten.ts` | Copy + adapt (done) | Walks `HistoryRoot` over `StubRef` segments; `FlatHistoryEntry.ref` field |
| `breadcrumb/BreadcrumbBar.tsx` | Copy + adapt (done) | Takes `chain: ReadonlyArray<StubRef>`, `historyRoot`, `panelId`; renders history-root trigger + chain entries |
| `breadcrumb/BreadcrumbEntry.tsx` | Copy + adapt (done) | Resolves `ref: StubRef` to concrete stub via `useConnector`; dispatches `InlineLink`; click → `activateChainEntry`; right-click → `restoreIntoNewPanel`; drag → `useDraggableChainEntry` |
| `breadcrumb/HistoryRootMenu.tsx` | Copy + adapt (done) | Empty-state text now "Opened from the roots panel." |
| `breadcrumb/HistoryRootEntry.tsx` | Copy + adapt (done) | Resolves `entry.ref` to concrete stub; click/drag → `restoreIntoNewPanel(computeOpenNewFromHistoryEntry(entry))` |
| `workstation/navigate.ts` | Rewrite as `shell/WorkstationApi.ts` (done) | `WorkstationApi` signature updated; `target: StubRef`, `restoreIntoNewPanel(state: RefPanelChainState)` |
| `workstation/WorkstationContext.tsx` | Copy (done) | Moved to `shell/WorkstationContext.tsx` |
| `workstation/PanelContext.tsx` | Copy + adapt (done) | Moved to `panel/PanelContext.tsx`; `kind: "ref" \| "roots"`; chain typed `ReadonlyArray<StubRef>` |
| `workstation/NamespaceContext.tsx` | Drop (done) | |
| `interactions/drag.ts` | Rewrite (done) | Moved to `reflink/drag.ts`; payloads carry `target: StubRef`; new MIME `application/x-fxtr-ref` |
| `interactions/HoverContext.tsx` | Copy (done) | Moved to `reflink/HoverContext.tsx`; verbatim |
| `interactions/useDraggableEntity.ts` | Rewrite as `useDraggableRef.ts` (done) | Takes `stub: EntityRefStub \| ServiceRefStub` |
| `interactions/useDraggableChainEntry.ts` | Copy + adapt (done) | Operates over `RefPanelChainState`; chain entry is `StubRef` |
| `interactions/useDraggableHistoryEntry.ts` | Copy + adapt (done) | `FlatHistoryEntry` now `StubRef`-based |
| `interactions/useEntityClick.ts` | Rewrite as `useRefClick.ts` (done) | |
| `interactions/useEntityHoverCard.tsx` | Rewrite as `useRefHoverCard.tsx` (done) | Dispatches `HoverPreview` via `RenderDispatch stub={stub}` |
| `interactions/useEntityContextMenu.tsx` | Rewrite as `useRefContextMenu.tsx` (done) | |
| `interactions/DraggableEntityHandle.tsx` | Rewrite as `DraggableRefHandle.tsx` (done) | |
| `panels/NamespacePanel.tsx` | Rewrite as `RootsPanel.tsx` (done) | Stateless info panel — no session. Reads from `connector.listEntities()` / `listServices()`; filters to records with `external_names.length > 0`; renders entities + services in two sorted sections |
| `panels/namespacePanelComponent.ts` | Rewrite as `RootsPanelParams.ts` (done) | `{kind:"roots"}` + `ROOTS_PANEL_COMPONENT` constant. No sessionId — info panels don't carry one |
| `sidebar/OpenPanelsSidebar.tsx` | Copy + adapt (done) | Row component switches on `panel.component`; ref-panel rows read active stub via `PanelSessionRegistry.sessions` + the panel session's `userState` atom; info-panel rows show plain title |
| `sidebar/ResizableSidebar.tsx` | Copy (done) | Verbatim port |
| `sidebar/usePanelLayout.ts` | Copy + adapt (done) | Simplified — no longer extracts the active stub at this level; just returns groups + panels with id / component / title / isActive |
| `sidebar/SidebarTooltip.tsx` | Copy + adapt (done) | Takes `stub: EntityRefStub \| ServiceRefStub` instead of `entity: FxtrID`; uses `RenderDispatch as={HoverPreview}` |
| `components/JsonTree.tsx` | Copy + adapt | Renders both stub kinds inline |
| `fallbacks/FullPanelFallback.tsx` | Rewrite (done) | Split into `EntityFullPanelFallback` + `ServiceFullPanelFallback`; both show shortId + "renderer not configured" message (JsonTree integration deferred to a later chunk that builds a load helper) |
| `fallbacks/BlockLinkFallback.tsx` | Copy + adapt (done) | Split into entity + service variants; service uses "SERVICE" tag |
| `fallbacks/InlineLinkFallback.tsx` | Copy + adapt (done) | Split; service prefixes `svc:` |
| `fallbacks/HoverPreviewFallback.tsx` | Copy + adapt (done) | Split; service shows "service · no preview configured" |
| `fallbacks/PanelErrorBoundary.tsx` | Copy (done) | Verbatim port |
| _new_ `fallbacks/DispatchErrorFallback.tsx` | New | Shared error fallback for both kinds; cast per-(stub,props) in registry helper |
| _new_ `panel/PanelSessionLifecycle.ts` | New (done) | `usePanelSession` hook driving pending/live/unavailable per panel — attach-only (shell mints sessions) |
| _new_ `panel/RefPanelParams.ts` | New (done) | `{kind:"ref", sessionId: string \| null}` panel-params type + `REF_PANEL_COMPONENT` constant |
| _new_ `panel/SessionUnavailable.tsx` | New (done) | Body shown on `lifecycle === "unavailable"` |
| _new_ `shell/ConnectorContext.tsx` | New (done) | `useConnector()` — provided by the shell, consumed by `PanelWrapper` + `RootsPanel` |
| _new_ `shell/PanelSessionRegistry.tsx` | New (done) | Shell-internal context for tracking each panel's live session by dockview panel id; consumed by `PanelWrapper` (register on live) and `WorkstationApi` (lookup for navigation writes) |

**Cross-package edits in chunk 3.**

- `@fxtr-ui/core/src/session.ts` — added `"unavailable"` to
  `SessionLifecycleState` union with JSDoc.
- `@fxtr-ui/connector-workstation/src/session.ts` — updated FSM
  doc + transitions; `subscribe_failed` now routes to
  `markUnavailable` instead of `markClosed`; renamed
  `isClosed()` → `isTerminal()` and `setLifecycleIfNotClosed` →
  `setLifecycleIfNotTerminal`; `assertWritable` reports the
  specific terminal state in its error message.

We'll track per-file migration progress in this table as chunks
land. "Copy + adapt" means start from the old file and refactor
imports + types, rather than re-deriving.

## Workflow notes

- **Subagent delegation.** With `CLAUDE_CODE_FORK_SUBAGENT`
  enabled, the easiest way to parallelize copy+adapt work is to
  fork (Agent with no `subagent_type`) — each fork inherits this
  conversation's context and can write files in isolation. Good
  candidates: the eight default-fallback components in chunk 1,
  the seven interaction-hook files in chunk 2, the breadcrumb
  ports in chunk 5, the sidebar ports in chunk 7. Within a chunk
  the forks should be told the *exact* file list they own and any
  cross-file conventions (imports, naming, theme classnames) so
  the output stitches back together cleanly.
- **Per-file migration ticks.** As files migrate, mark them in
  the migration table above with `(done)` or `(stale: see <new
  path>)` so we can tell what's left without diffing.
- **Test gating.** Per the project plan, tests come last —
  individual chunks may break `pnpm test` until chunk 10. They
  should still keep `pnpm tsc --build` clean.

## Chunks

### Chunk 1 — Package scaffold, dispatch keys, default fallbacks

Stand up the new `@fxtr-ui/workstation` package with `package.json`,
`tsconfig.json`, theme CSS dependency wiring, and the dispatch-side
surface only:

- The four new `DispatchKey` declarations in `dispatch/keys.ts`
  with their Props types.
- `createWorkstationDispatchRegistry(options)` in
  `dispatch/registry.ts` that builds a `DispatchRegistry` with the
  consumer's entries + auto-installed entity / service default
  fallbacks via `staticEntityTypeDispatch` /
  `staticServiceTypeDispatch`.
- The eight default fallback components
  (`{Entity,Service}{FullPanel,BlockLink,InlineLink,HoverPreview}Fallback`).
  These are static, no real interactions yet — they just show
  shortId / "service" labels with theme classes.
- `PanelErrorBoundary` carried over.

Reviewable as: "the dispatch surface for this stage." Visual review
target: `pnpm tsc --build`; no runtime.

### Chunk 2 — Navigation surface, `RefLink`, interaction hooks

Wire the consumer-side surface that panels and dispatched
components use to interact with refs and trigger navigation:

- `shell/WorkstationApi.ts` — the `WorkstationApi` interface,
  `FollowLinkEvent`, `DropPosition`, etc.
- `shell/WorkstationContext.tsx` — context + `useWorkstation()`.
- `panel/PanelContext.tsx` — context + `usePanelContext()`.
- `reflink/drag.ts` — discriminated `DragPayload`, MIME constants,
  helpers.
- `reflink/HoverContext.tsx` — port verbatim.
- `reflink/RefLink.tsx` — public component, dispatches on stub kind.
- The four hooks (`useRefClick`, `useRefHoverCard`,
  `useRefContextMenu`, `useDraggableRef`) + the chain / history
  variants + `DraggableRefHandle`.

`WorkstationApi`'s methods are typed but no implementation exists
yet — the hooks invoke them through context, which will be
provided by chunk 4. Hooks branch on `usePanelContext()` like today:
panel-context callers go through `followLink`, non-panel callers go
through `restoreIntoNewPanel`.

Reviewable as: "this is what a consumer of refs interacts with."

### Chunk 3 — Per-panel session lifecycle + `PanelWrapper`

Build the panel content renderer and the session lifecycle around
it:

- `panel/PanelSessionLifecycle.ts` — `usePanelSession({connector,
  sessionId})` hook driving an **attach-only** pending → live →
  unavailable state machine. No session creation in the panel —
  the shell mints sessions out-of-band and writes the `sessionId`
  back into dockview params; the hook observes the change and
  attaches.
- `panel/PanelWrapper.tsx` — the panel content renderer registered
  with dockview. Drives the lifecycle hook; on `live`, wraps in
  `<FxtrSessionProvider>`, provides `PanelContext`, and dispatches
  on the active stub (after reading `chain` / `activeIndex` from
  session userState). Passes a `useSubAtom`'d `ownStorage` to
  FullPanel.
- `panel/SessionUnavailable.tsx` — render-time body for the
  `unavailable` case.

No actual `WorkstationApi` implementation yet (chunk 4); panel
interactions resolve to no-ops at this stage. But `PanelWrapper`'s
own internals — pending state, session attachment, chain reads —
work standalone.

### Chunk 4 — `WorkstationShell` + navigation implementation

Tie it all together:

- `shell/DockviewLayout.tsx` — registers `PanelWrapper` for each
  `PanelKind`, wires drop handling.
- `shell/WorkstationShell.tsx` — top-level component.
  Responsibilities:
  - Hold the connector reference (consumer prop), mint/attach the
    dispatcher session synchronously on mount.
  - Maintain the panel-id counter (`panel-<n>`).
  - Implement `WorkstationApi` against dockview + per-panel
    sessions:
    - `followLink` — for "replace-in-source", computes
      `nextChainState` and writes it to the source panel's session
      via `session.userState` (so persistence is automatic). For
      "open-new", mints a new panel with a pending session.
    - `activateChainEntry` — locates the target panel, writes the
      new `activeIndex` into its session.
    - `restoreIntoNewPanel` — mints a new panel with a pending
      session seeded by the snapshot's chain.
  - Provide `<DispatchProvider session={dispatcherSession}>` +
    `<WorkstationContext.Provider>`.

After this chunk, the shell can be mounted manually and a single
roots-less, panel-less layout works. Panel-level dispatch resolves
to fallback components (no real handlers yet).

### Chunk 5 — Breadcrumb + history-root menu

- `breadcrumb/types.ts` — generalized types.
- `breadcrumb/BreadcrumbSourceContext.tsx`, `history.ts`,
  `flatten.ts` — ported / rewritten.
- `breadcrumb/BreadcrumbBar.tsx`, `BreadcrumbEntry.tsx`,
  `HistoryRootMenu.tsx`, `HistoryRootEntry.tsx` — render top-of-
  panel chrome reading from the panel's session via context +
  `useSubAtom`.

`PanelWrapper` now renders `BreadcrumbBar` above the dispatched
FullPanel.

### Chunk 6 — Roots panel (registry browser)

- `panels/RootsPanel.tsx` — subscribes to
  `connector.listEntities()` and `connector.listServices()`,
  filters to records with an `external_name`, renders each as a row
  containing the external name + a `<RefLink as="block">`. Two
  sections (entities / services); each section sorted by name.
- `panels/rootsPanelComponent.ts` — panel kind constant +
  registration.

The roots panel is the default opening view if no layout is
persisted. Like other panels it owns a session, but its session
userState stays empty.

### Chunk 7 — Open-panels sidebar

- `sidebar/OpenPanelsSidebar.tsx`, `usePanelLayout.ts`,
  `ResizableSidebar.tsx`, `SidebarTooltip.tsx` — port from the old
  code with stub generalization. The sidebar reads each panel's
  active stub from its session userState (via the connector's
  session attach, which the sidebar reuses — not a fresh
  subscription per row). Row labels and tooltip previews dispatch
  the appropriate non-interactive key.

### Chunk 8 — Layout persistence v4

- `persistence/layout_v4.ts` — serialize/deserialize schema.
  Migration from absent / v3 → fresh layout (no in-place upgrade;
  v3 stored entity ids, the new shell has nothing meaningful to
  do with them).
- `persistence/useLayoutPersistence.ts` — boot-time restore + 200ms
  debounced save on dockview events.
- Wiring in `WorkstationShell`. Test that reloading the demo's
  browser tab restores the panels at the right session IDs.

### Chunk 9 — Demo backend + Vite dev client

- New `frontend_lib/packages/fxtr-ui-workstation-demo/` package
  (sibling to the existing `@fxtr-ui/*` demos).
- `src/demo_workstation.py` — boots `WorkstationServer`, opens a
  peer-RPC connection to its own `/rpc`, registers `DemoNote` /
  `DemoSection` / `DemoArchive` / `DemoListNode` entities under
  external names via `expose_entity`, registers a couple of toy
  services (echo + counter, or one stub `_DemoServiceImpl`) via
  `expose_service`. Prints `READY <ws://...>` and parks.
- `src/server.ts` — `tsx`-runnable orchestrator: spawns the Python
  side, reads `READY`, starts Vite at port 3463 with
  `VITE_WORKSTATION_URL` set.
- `src/client/main.tsx`, `App.tsx` — `WorkstationConnector` against
  the env URL; mount `<WorkstationShell connector={...}
  registry={...} />`.
- `src/client/handlers.tsx` — handler entries for the demo entity
  types via `staticEntityTypeDispatch`. Equivalent to today's
  `example-dev-workstation/src/client/handlers.tsx` but built on
  the new dispatch keys.

At this point the demo opens in a browser at visual parity with
today (modulo intentional rename-related label changes).

### Chunk 10 — Tests

Vitest unit tests for the parts that have settled:

- `panel/PanelSessionLifecycle.test.ts` — pending → live, pending →
  missing, terminate-on-unmount.
- `breadcrumb/history.test.ts` — pure transforms.
- `persistence/layout_v4.test.ts` — round-trip + migration drop.
- `reflink/drag.test.ts` — payload encode/decode.

Anything React-component-level may need to ride in a sibling tests
package per the `@fxtr-ui/react`-tests precedent. Test discovery
goes via the existing `v2/ts/` workspace's `pnpm test`.

## Deferred / out of scope

- **HTTP connector integration.** Stage 5 still deferred. The demo
  is workstation-only.
- **Real service renderers.** Service-typed dispatch entries fall
  back to the default service fallback components shipped by this
  package. Real renderers are stage 9 territory.
- **Service invocation UX from panels.** Beyond the fact that
  services *render* in panels (as fallbacks), there's no UI for
  invoking methods on them. That's stage 9 too.
- **"Recently closed tabs" recovery.** Closing a panel terminates
  the session; reopening is not surfaced by the shell. Could come
  back if we add per-session retention server-side.
- **Atom-family-keyed-by-session-id helpers.** The shared-store
  model lets us add these if we want per-panel in-memory atoms
  beyond what session state covers. Not needed for the deliverable.

## Open questions / things to revisit

- **Name of the new lifecycle state.** Working name is
  `"unavailable"`. Alternatives: `"nonexistent"` (describes
  server-side state more literally), `"missing"`. Decide as part
  of chunk 3.
- **`ownStorage` typing strictness.** Typed `FxtrSessionAtom<unknown>`
  intentionally — the consumer (`PanelWrapper`) has no claim on the
  shape. Worth verifying that `useSubAtom` chains compose cleanly
  on the renderer side without runtime casts beyond the one
  consumers already accept (see the `useSessionRootStateAtom` cast
  note in `@fxtr-ui/react`).
- **Where service handlers will live in stage 9.** If we expect
  the same `staticServiceTypeDispatch` machinery, the API surface
  in `dispatch/registry.ts` should be symmetric — entity entries
  and service entries are both first-class. Stage 8 should ship
  the symmetric API even though the demo only fills in entity
  entries.
- **Closing panels in error state.** If a panel's session creation
  errors out (network drop during boot), what does close-via-X do?
  Probably just unmount, no terminate. Edge case to nail down in
  chunk 3.
- **Dispatcher session: stable ID across deploys?** Today we
  create-and-persist. If we want shared dispatch state across
  multiple browsers / users hitting the same workstation, we'd
  want a named singleton. Stage 8 stays with per-tab dispatcher
  session; cross-tab sharing is a separate decision.

## Cleanup log

Running list of things to revisit at the end of the stage — small
debts that don't justify their own commit now but should not be
forgotten. Items are added as we go; the end-of-stage audit
clears them.

- **`SessionUnavailable` reopen button.** Currently informational
  only — no affordance to re-mint a session. Wire a button that
  calls a shell-provided `reopenPanel(panelId)` callback (which
  re-runs the shell's session-minting flow against this panel's
  dockview id); the panel's `sessionId` goes `<gone> → null →
  <new>` and `PanelWrapper` re-attaches naturally.
- **Dispatcher session failure has no inline retry.** If the
  dispatcher `createSession` fails (network drop / unreachable
  server during the very first boot), there's no in-tree retry
  affordance. The user has to reload. Acceptable for now;
  could add a retry button that re-runs the `createSession`
  flow.
- **Hard render gate on dispatcher `connected`.** The dispatch
  state machines call `ctx.allocateLoad` which throws while
  `connecting`. The shell currently waits for `connected` before
  rendering the tree. If the resulting latency at boot is
  annoying, an alternative is making the static dispatch helpers
  tolerate `connecting` (await `connected` inside their effects).
- **`RefSidebarRow` subscribes to the full sessions map.** Each
  ref-panel row in the sidebar reads `useAtomValue(panelRegistry.sessions)`,
  so every register / unregister event re-renders every ref row.
  Fine for typical panel counts; if it becomes a perf concern,
  switch to a per-`panelId` derived atom (atomFamily-style) so a
  row only re-renders when *its* session pointer changes.
- **`SessionPendingBody` / `EmptyChainBody` wording.** Once the
  shell seeds chain state on panel open, the empty-chain body
  should never render in normal use. If it persists as a real
  fallback path, the language should be revisited.
- **Open-new-panel does attach-detach-reattach.** Each new ref
  panel does two subscribe round-trips: shell creates session +
  writes seed + detaches, then `PanelWrapper` re-attaches. Could
  be one round-trip if the shell handed the concrete session
  reference to the panel via a one-shot context (e.g.
  `<PanelInitialSessionContext value={{panelId, session}}>`
  consumed by `PanelWrapper`'s lazy init). Open-panel latency
  is dominated by `createSession` itself today, so not urgent.
- **No connection-status indicator in the shell.** The connector
  exposes `connectionStatus` (`connecting / open / reconnecting
  / closed`) but the shell never surfaces it. WebSocket drops
  are invisible to the user beyond panels going quiet. A small
  badge in the debug-UI header bar (next to the link) would do.
- **Dispatcher session lives in shell-local React state.** Works
  fine for our single-shell-per-page model. The
  architecturally-cleaner shape would be a module-level
  singleton + observable so the React tree can mount / unmount
  freely without touching the session. Not biting us; note for
  future.

## Reference (from the original stub)

- The existing
  [`projects/fxtr_workstation/ts/CODE_OVERVIEW.md`](../../../ts/CODE_OVERVIEW.md) —
  the architecture being ported.
- [`design_notes/02_connector_abstraction.md`](./design_notes/02_connector_abstraction.md) —
  session model this layers on.
- [`design_notes/05_dispatch_and_inline_loading.md`](./design_notes/05_dispatch_and_inline_loading.md) —
  dispatch pattern for panel content; ownStorage rationale.
- [`_demo_workstation.py`](../../../frontend_lib/packages/fxtr-ui-connector-workstation/tests/_demo_workstation.py) —
  the peer-RPC self-loopback pattern the demo backend follows.
