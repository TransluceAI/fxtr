# 06 — Shell decoupling: the PanelViewer refactor

Status: resolved (Q-C + Q-D, generalized per Daniel's "carve at the
joints" note). Lands as **Part 4**. The cut is not "extract an API so
the existing workstation UI talks to a new connector" — it's splitting
the **generic panel-viewer mechanics** from the **connector/app-specific
panels**.

> **Correction (2026-06-15): `@fxtr-ui/workstation-dispatch` already
> exists and is already connector-agnostic.** This doc (and the Part 4
> doc) were originally drafted against a stale `@fxtr-ui/workstation`
> `LOCAL_CODE_OVERVIEW` that still claimed `dispatch/`, `metadata/`, and
> the entity/service fallbacks lived *inside* the shell. They don't —
> they were long ago externalized into `@fxtr-ui/workstation-dispatch`
> (depends only on `@fxtr-ui/core` / `react` / `react-dispatch` /
> `state-util` / `jotai`; **no connector, no dockview**), so that
> dynamically-loaded renderer modules share one identity per dispatch
> key. That package already owns the **dispatch keys + registry +
> fallbacks**, the **`RefLink`** component (presentational), the
> **metadata views** (`EntityMetadataView` / `ServiceMetadataView` /
> `JsonRefTree`), the **dnd primitives**, and — crucially — three
> **injection contexts** that are exactly the abstractions this refactor
> wanted to invent:
>
> | Context (in `workstation-dispatch`) | What it abstracts | Concrete impl today |
> |---|---|---|
> | `RefLinkImplContext` / `RefLinkImpl` | interaction behaviour around a dispatched link | `fxtr-ui-workstation/src/reflink/ShellRefLinkImpl.tsx` |
> | `RefStubResolverContext` / `RefStubResolver` | `entityRef` / `serviceRef` / **`classifyShortId`** | `fxtr-ui-workstation/src/reflink/ShellRefStubResolver.tsx` |
> | `WorkstationNavigateContext` | the navigate-from-a-ref action | the nav hook in `ShellRefLinkImpl` (routes via `WorkstationApi`) |
>
> Consequences for the plan below: (a) there is **nothing to extract**
> for dispatch rendering / `RefLink` / metadata views — they're already
> in a shared, connector-agnostic package; (b) `classifyShortId` already
> has a home (the `RefStubResolver` interface) — the only change is to
> have the resolver answer it from `connector.classifyShortId` instead of
> a list scan; (c) what genuinely moves into the new
> `@fxtr-ui/panel-viewer` is the **dockview shell mechanics + the
> concrete reflink interaction impls** (`ShellRefLinkImpl` /
> `ShellRefStubResolver` + the interaction hooks), all of which are
> connector-agnostic in principle and couple to `@fxtr-ui/connector-workstation`
> only via `import type`. The packaging section below is updated
> accordingly; the touchpoint table's "bucket" column still holds, but
> read "dispatch rendering / RefLink / metadata" as **already external**,
> not as things to move.

## Why a real split, not a feature-detected interface

The earlier sketch (a portable `UiConnector` with optional
workstation-shaped members the two debug panels feature-detect) put
backend-specific lumps into a neutral interface and still shipped
workstation-specific UI in the shell. The cleaner boundary, confirmed
by the touchpoint survey below: the shell's **core is already
connector-agnostic** (per-panel sessions, chain/breadcrumb/history
navigation, ref-link routing, dispatch rendering, layout persistence
know nothing about roots/debug panels); the coupling is entirely in
the **orchestration layer** — a fixed panel-component map in
`DockviewLayout.tsx` and hard-coded open-panel calls in
`WorkstationShell.tsx`.

So: a generic **PanelViewer** that owns the core mechanics and takes
**injected panel registrations**, with roots + debug panels becoming
connector-specific registrations. Consequence: there is **no shared
list/record abstraction** — `listEntities` / `listServices` /
`UiEntityRecord` / `UiServiceRecord` are dropped from any portable
interface (each connector's own roots panel uses its own connector's
lists and record types).

## Touchpoint inventory (what the shell consumes today)

Everything in `frontend_lib/packages/fxtr-ui-workstation/src/` that
touches `@fxtr-ui/connector-workstation`:

| Site | Member used | Bucket |
|---|---|---|
| `shell/WorkstationShell.tsx:191-224` | props take `connector: WorkstationConnector` | → the shared `Connector` |
| `shell/WorkstationShell.tsx:268,320,381` | `store`, `createSession(metadata)`, `attachSession(id)` | **core** |
| `panel/PanelSessionLifecycle.ts:80` | `attachSession(id)` | **core** |
| `shell/ShellHeader.tsx:42,54` | `connectionStatus`, `url` | **core** (header) |
| `reflink/ShellRefStubResolver.tsx:34-44` | `entityRef`, `serviceRef`, `listEntities()`/`listServices()` for `classifyShortId` | **core** for refs; `classifyShortId` moves onto the connector (no lists) |
| `types.ts:146`, `reflink/drag.ts:158` | `entityRef`/`serviceRef` | **core** |
| `panels/RootsPanel.tsx:84-148` | `listEntities()`, `listServices()`, `entityRef`, `serviceRef` | **injected (connector-specific)** |
| `panels/WorkstationDebugPanel.tsx:35-38` | `listRpcConnections/Services/Entities/Sessions({debug:true})` | **injected (workstation-only)** |
| `panels/SessionDebugPanel.tsx:74-138,347-458` | `attachSession`, `connection.registerSubscription({kind:"session"\|"session_debug"})`, debug lists, refs | **injected (workstation-only)** |
| `panels/DispatcherDebugPanel.tsx` | `dispatcher.debugView()` | **injected (dispatcher)**; Part 1 adds a manual reload action here |

Also storage-coupled: `persistence/layout_v6.ts:44-122` (hardcoded
`localStorage`, embeds **sessionIds**) and
`persistence/dispatcher_session_id.ts` — both switch to a passed-in
**`Storage`** handle (the standard Web Storage interface, design/04 —
not a custom type) so the HTTP app can pass *one* handle to both the
connector and the PanelViewer and have its shortId table, session
blobs, and layout age together. The workstation app passes a handle to
the PanelViewer only.

## The shared `Connector` interface

This is the right moment to define the **common connector abstraction**:
there are now two implementations (`WorkstationConnector`,
`HttpConnector`) to ground it, and any viewer frontend — not just this
dockview PanelViewer — needs the same post-construction surface from a
backend. It lives in `@fxtr-ui/core` as a sibling to the abstract
`Session` (core is the abstract-contracts layer; this is *not* a
PanelViewer-specific type).

```ts
interface Connector {
  store: JotaiStore;
  url: string;                              // display label
  connectionStatus: Atom<ConnectionStatus>;
  entityRef<T>(shortId: string): EntityRefStub<T>;
  serviceRef<T>(shortId: string): ServiceRefStub<T>;
  classifyShortId(shortId: string): "entity" | "service" | null;
  loadEntityData<T>(stub: EntityRefStub<T>): Promise<T>;
  createSession(metadata: JsonValue): Promise<Session>;
  attachSession(sessionId: string): Session;
  close(): void;
}
```

`WorkstationConnector` and `HttpConnector` are declared `implements
Connector` (a compile-time conformance guarantee); their extra members
(the workstation's `{debug}` lists, raw `connection`, etc.) are
supersets that don't break assignability. The PanelViewer types
against `Connector`. (Naming parallels `Session` ← `WorkstationSession`.)

This is the **consumer-facing** surface only — the **constructor** side
stays per-connector (each keeps bespoke construction args), which is
the split `design_notes/02_connector_abstraction.md` deliberately
delayed defining until a second connector existed.

No `UiEntityRecord` / `UiServiceRecord` / `ReactiveList` on `Connector`
— those stay with whichever concrete connector + roots panel use them.

**`classifyShortId` is a connector method, not a list scan.** Today
`ShellRefStubResolver.classifyShortId`
(`reflink/ShellRefStubResolver.tsx:39-49`) scans `listEntities` /
`listServices` records to decide entity-vs-service. That's the *only*
ref-link consumer of the lists — `RefLink` itself always renders the
bare `shortId` (external names are read **only** by `RootsPanel`). So
we move `classifyShortId` onto the connector: the `HttpConnector`
answers it **authoritatively from its shortId table** (which records
each shortId's kind — no prefix-guessing), and `WorkstationConnector`
keeps its current list-backed scan. Ref-link rendering is then
identical with or without portable lists, and the dispatch
ref-resolver (chip content, placeholder substitution) keeps working.

(Per the correction above, this abstraction already half-exists: the
`RefStubResolver` interface in `@fxtr-ui/workstation-dispatch` already
declares `classifyShortId`. The change is just that `ShellRefStubResolver`
delegates to `connector.classifyShortId` rather than scanning lists
itself — so the work is "add `classifyShortId` to `WorkstationConnector`
+ point the resolver at it", not "invent a resolver".)

## Panel registrations

The PanelViewer takes a registry instead of importing panels:

```ts
interface PanelRegistration {
  id: string;                                  // dockview component key
  title: string;
  component: React.ComponentType<PanelProps>;  // closes over the app's connector
  // surfacing:
  sidebarButton?: { label: string };           // find-or-open from the sidebar
  // (omit ⇒ hidden; opened programmatically from another panel)
  forSession?: boolean;                         // per-session variant (e.g. session-debug)
}
```

- The PanelViewer renders a sidebar button per registration that has
  one (`+ Roots`, `+ Debug`, …), wiring it to find-or-open. Hidden
  registrations are opened programmatically via a small panel-opening
  API the PanelViewer exposes to panels (`openPanel(id, params?)`),
  which is how a workstation-debug panel opens a per-session debug
  panel today (`workstation.openSessionDebugPanel`).
- The built-in **ref panel** (`PanelWrapper` + the chain/breadcrumb/
  navigation machinery) stays internal to the PanelViewer — it *is*
  the core and is always present.
- Dispatch rendering (FullPanel) stays internal too; the dispatcher
  is still injected as today.

## Packaging (resolved — four layers, given `workstation-dispatch` already exists)

- **`@fxtr-ui/core`** — abstract `Connector` + `ConnectionStatus`,
  abstract `Session`, stubs, `memoryStorage`, escape codec. (Part 3.)
- **`@fxtr-ui/workstation-dispatch`** (already exists, already
  connector-agnostic — **no change in Part 4** beyond possibly a doc/name
  note) — dispatch keys + registry + fallbacks, `RefLink` + the three
  injection contexts (`RefLinkImplContext`, `RefStubResolverContext`,
  `WorkstationNavigateContext`), metadata views, dnd primitives.
- **`@fxtr-ui/panel-viewer`** (new package) — the **dockview shell
  mechanics**: the `PanelViewer` component (was `WorkstationShell`),
  `DockviewLayout`, per-panel session lifecycle (`PanelWrapper` +
  `usePanelSession`), chain/breadcrumb/history navigation, the sidebar,
  layout persistence (through an injected `Storage`), the
  `PanelViewerApi` (was `WorkstationApi`), the registration types + the
  `openPanel` API, **and the concrete reflink interaction impls**
  (`ShellRefLinkImpl` / `ShellRefStubResolver` + the interaction hooks
  `useRefClick` / `useRefContextMenu` / `useRefHoverCard` /
  `useDraggable*` / `DebugRefChip`) moved out of the shell and retyped
  against the abstract `Connector`. Depends on `@fxtr-ui/core` +
  `@fxtr-ui/workstation-dispatch` (+ `react-dispatch`, `state-util`,
  `dockview`, `@base-ui/react`, `jotai`).
- **`@fxtr-ui/workstation`** — reduced to the **four panels as
  registrations** (`RootsPanel`, `WorkstationDebugPanel`,
  `SessionDebugPanel`, `DispatcherDebugPanel`) + their debug tables.
  Depends on `@fxtr-ui/panel-viewer` + `@fxtr-ui/connector-workstation`
  (the panels use the connector's lists / debug records) +
  `@fxtr-ui/workstation-dispatch`. (`WorkstationConnector` itself stays
  in `@fxtr-ui/connector-workstation`, where it already lives — it does
  *not* move into `@fxtr-ui/workstation`.)
- The **HTTP demo** depends on `@fxtr-ui/panel-viewer` +
  `@fxtr-ui/workstation-dispatch` + its own panels +
  `@fxtr-ui/connector-http` — and **never** on `@fxtr-ui/workstation`.

**Naming wart (decision deferred):** `workstation-dispatch` and several
of its exports (`WorkstationNavigateContext`, `createWorkstationDispatchRegistry`)
keep the "workstation" prefix despite being generic. Renaming them would
churn the dynamic-dispatch import-map externals (renderer modules import
these by identity), so Part 4 **does not** rename `workstation-dispatch`
internals; it only renames the shell-level symbols that move into
`panel-viewer` (`WorkstationShell` → `PanelViewer`, `WorkstationApi` →
`PanelViewerApi`, `WorkstationContext` → `PanelViewerContext`). The
`workstation-dispatch` rename is left as a separate future cleanup.

## Where the existing panels go

The four current panels move out of the generic core into
**workstation-specific registrations** in `@fxtr-ui/workstation`,
built by the workstation demo and handed to the PanelViewer:

- **RootsPanel** → a workstation roots panel over
  `WorkstationConnector.listEntities/listServices`. The HTTP demo
  ships its **own** roots panel over `HttpConnector`'s lists (records
  carrying `service_type_id` from discovery, design/01).
- **WorkstationDebugPanel** / **SessionDebugPanel** → workstation-only
  registrations (debug-projected lists + raw
  `connection.registerSubscription`). The HTTP demo can ship its own
  debug affordances instead — e.g. a **storage-inspector** panel a
  workstation can't offer.
- **DispatcherDebugPanel** → a dispatcher-specific registration
  (already only needs `dispatcher.debugView()`); Part 1 adds the
  manual "reload everything" action to it.

## Effort

Per the structure survey, the work splits into **mechanical moves** and
a small amount of **new logic**:

- *Mechanical (the bulk):* relocate the shell mechanics + the reflink
  interaction impls from `@fxtr-ui/workstation` into the new
  `@fxtr-ui/panel-viewer` (a large diff by line count, but mostly file
  moves), retype their `WorkstationConnector`/`WorkstationSession`
  `import type`s to the abstract `Connector`/`Session` (a pure type-swap
  — runtime already flows via `<ConnectorProvider>`), and rename
  `WorkstationShell`→`PanelViewer` / `WorkstationApi`→`PanelViewerApi` /
  `WorkstationContext`→`PanelViewerContext` across importers.
- *New logic (small, ≈200-300 lines):* the registration type, making
  `DockviewLayout` register components from the registry instead of
  importing them, replacing the hard-coded open-panel calls with the
  registry-driven sidebar + `openPanel`, adding `classifyShortId` to
  `WorkstationConnector` (+ pointing `ShellRefStubResolver` at it), and
  threading the injected `Storage` into layout persistence.

The dispatch/RefLink/metadata layer needs **no work** — it's already in
`@fxtr-ui/workstation-dispatch`. Detailed step list is the Part 4 doc's
job; land it in reviewable chunks (mechanics move first, behavior
identical, then flip the four panels to registrations).
