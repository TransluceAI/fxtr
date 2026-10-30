# Step 3 — `@fxtr-workstation/core` MVP + tiny dev demo

Build the first runnable slice: dispatch-key declarations, fallback
renderers (including an adapted `JsonTree` for the `FullPanel`
fallback), the `WorkstationShell` component wrapping a single dockview
panel, and a tiny dev demo server + client that exercises the whole
stack end-to-end.

By the end of step 3 we should be able to run `pnpm dev` in the new
demo package, open the browser, and see a dockview with a single
panel rendering a real entity (via a registered handler for one type,
and via the JsonTree fallback for a type we intentionally leave
unregistered).

## Decisions (resolved)

From discussion before writing this plan:

- **`setPanelTitle` accepts `string`.** `ReactNode` would require a
  custom dockview tab component; postpone.
- **Default panel title: `entity.idString().slice(-8)`.** No `#`
  prefix; fxtr's `FxtrID` doesn't expose a human `shortId`/`typeName`
  out of the box and we don't need one for MVP.
- **Live-rendering surface: a new dedicated dev demo package.** Tiny
  server with 2–3 hardcoded entity types — independent of the
  fingerprinting demo, so we don't accidentally pollute the step-7
  setup with placeholder code.
- **Dockview CSS imports live in demo code, not in workstation-core.**
  Keeps the library free of `declare module "*.css"` shims; consumers
  import `dockview/dist/styles/dockview.css` +
  `@fxtr-workstation/theme/all.css` at their entry.
- **Fallback FullPanel uses an adapted `JsonTree`.** Port
  thingwatcher's `frontend/components/DetailsPanel/JsonTree.tsx` and
  upgrade it to understand `FxtrCbor` (tagged FxtrIDs render as
  nested `<Render entity={id} as={BlockRef} />` so child references
  are still clickable/hoverable through the dispatch system).

## Package additions

### `fxtr-workstation-core/src/` — new files

```
src/
├── index.ts                        # re-exports everything below
├── keys.ts                         # 4 DispatchKey declarations
├── shortId.ts                      # shortId(entityId) → idString().slice(-8)
├── WorkstationShell.tsx            # top-level component (single-panel MVP)
├── DockviewLayout.tsx              # dockview container + panel-component registration
├── EntityPanelWrapper.tsx          # dockview IContentRenderer for entity panels
├── registry.ts                     # createWorkstationDispatchHandler(userEntries, options)
├── components/
│   └── JsonTree.tsx                # FxtrCbor-aware recursive tree view
└── fallbacks/
    ├── FullPanelFallback.tsx       # loads UnknownEntity, renders JsonTree
    ├── BlockRefFallback.tsx        # small labeled box
    ├── InlineRefFallback.tsx       # inline code-style token
    └── HoverPreviewFallback.tsx    # compact card (shortId + typeId)
```

No CSS imports inside workstation-core's source. Per-component styles
go via CSS modules or inline styles using our theme tokens
(`var(--accent)` etc.) and are only evaluated when the theme's
`tokens.css` has been loaded by the consumer.

### `example-dev-workstation/` — new package

A new sibling to `example-demo-fingerprinting-workstation`. Lean
scaffolding; independent of the fingerprinting server so this demo
can stay small through later phases.

```
packages/example-dev-workstation/
├── package.json                    # name: @fxtr-workstation/example-dev
├── tsconfig.json
├── vite.config.ts
├── index.html
└── src/
    ├── entities.ts                 # bicodec entity defs (see below)
    ├── server.ts                   # builds + serves data on :3460
    └── client/
        ├── main.tsx                # vite entry; side-effect CSS imports
        └── App.tsx                 # mounts WorkstationShell
```

Needs to be added to `tsconfig.json`'s `references` list.

## Dispatch keys (`keys.ts`)

Straight from the overview plan. Four keys, declared via
`declareDispatchKey` from `@fxtr/react-dispatch`:

| Key | Props beyond `entityRef` |
| --- | --- |
| `FullPanel` | `{ navState?: unknown; setPanelTitle: (title: string) => void }` |
| `BlockRef` | `{ linkText?: string }` |
| `InlineRef` | `{ linkText?: string }` |
| `HoverPreview` | `{}` |

Renderers' outer signature after dispatch-registry wrapping:
`({ entityRef: EntityRef<T> } & Props) => ReactElement`.

## `WorkstationShell`

MVP surface:

```ts
interface WorkstationShellProps {
  entity: FxtrID;
  // Optional override; default is shortId(entity).
  initialTitle?: string;
}
```

Must be inside `<FxtrProvider>` and `<DispatchProvider>`. Internally:

- Renders a `DockviewLayout` component filling its container.
- On mount (and whenever the `entity` prop changes), the shell's
  effect ensures a single panel exists for the current entity id;
  when the entity prop changes, the old panel is closed and a new
  one opened. Panel id = `entity-${entity.idString()}`.
- Applies `className="fw-dockview-theme"` on the dockview container
  (reads from our theme class — see step 2).

No sidebar, no namespace panel, no layout persistence yet. Those land
in step 4 / 5.

## `EntityPanelWrapper`

Implements dockview's content-renderer contract (React form). Reads
the entity `FxtrID` from `params.entity` and renders:

```tsx
<Render entity={id} as={FullPanel} setPanelTitle={setPanelTitle} />
```

`setPanelTitle` is safe during render (per overview's contract):

- Component holds a `titleRef = useRef<string | null>(null)`.
- `setPanelTitle(t)` just mutates the ref — never calls `setState`
  during render.
- A post-commit `useEffect` reads the ref and calls
  `api.setTitle(titleRef.current ?? shortId(entity))`. Runs after
  every render; `api.setTitle` is idempotent so this is fine.

If the handler never calls `setPanelTitle`, the effect still fires
with the default. If it calls it during render, the ref is populated
before the effect runs; one render → one title sync.

## Fallback renderers

All four live in `src/fallbacks/` and are registered by
`createWorkstationDispatchHandler` as catch-all entries using
`unknownLoadCodec` (which accepts any type).

- **`FullPanelFallback`** (`{ entityRef, setPanelTitle }`):
  - `useFxtrLoad(entityRef)` → `UnknownEntity`.
  - Calls `setPanelTitle(shortId(entityId))` (no-op since default is
    the same, but demonstrates the mechanism).
  - Renders `<JsonTree value={unknown.raw_cbor} />` inside a
    `.fw-panel__body`.
- **`BlockRefFallback`** (`{ entityRef }`): small inline-block with
  two tokens — "ref" tag + shortId. Uses `.fw-fallback-block-ref`
  class for styling (inline styles are fine too).
- **`InlineRefFallback`** (`{ entityRef }`): single `<code>` span
  showing shortId.
- **`HoverPreviewFallback`** (`{ entityRef }`): a compact two-line
  card — "shortId" monospace, "type: typeId.slice(-8)" muted.

None of the fallbacks use `useFxtrLoad` unless they need the payload
(only `FullPanelFallback` does). The other three work off
`entityId.idString()` alone.

## `JsonTree` — adaptation notes

Port thingwatcher's `frontend/components/DetailsPanel/JsonTree.tsx`.
Key changes:

- Input type goes from generic JSON to **`FxtrCbor`**.
- Add cases for `FxtrCbor`'s tagged-ID classes (imported from
  `@fxtr/core`): `RawWeakFxtrID`, `RawStrongFxtrID`,
  `RawFxtrEntityMarkerID`. For all three, render inline via
  `<Render entity={x.innerId} as={InlineRef} />`. That way the
  fallback FullPanel shows child refs as clickable / hoverable tokens
  and recursion into the dispatch system Just Works.
- Add a case for `Uint8Array` (CBOR bytes). Render as
  `bytes(N): hex-first-16...` in mono style.
- Drop thingwatcher-specific styling hooks; restyle using our theme
  tokens (colors: `var(--text-muted)` for keys, semantic status
  colors for booleans/nulls, `var(--text-accent)` for refs).
- Keep expand/collapse behavior, auto-collapse at depth > 2, key-count
  badge when collapsed.

## `createWorkstationDispatchHandler`

Thin wrapper around `createDispatchRegistry` from
`@fxtr/react-dispatch`:

```ts
function createWorkstationDispatchHandler(
  userEntries: RegistryEntry[],
  options?: { /* pass-through options */ },
): DispatchHandler {
  return createDispatchRegistry(
    [
      ...userEntries,
      // Catch-all entries using unknownLoadCodec — tried last.
      entry(FullPanel, unknownLoadCodec, FullPanelFallback),
      entry(BlockRef, unknownLoadCodec, BlockRefFallback),
      entry(InlineRef, unknownLoadCodec, InlineRefFallback),
      entry(HoverPreview, unknownLoadCodec, HoverPreviewFallback),
    ],
    options,
  );
}
```

Relies on `unknownLoadCodec` accepting all types. If it doesn't expose
`supportsEntityType`, we wrap it with a trivial `{ supportsEntityType:
() => true, decode: unknownLoadCodec.decode }` shim inside
`registry.ts`.

## Dev demo — entities, server, client

### Entities (`src/entities.ts`)

Minimal but interesting:

```ts
const DemoNote = bicodec.entity(defineType(NS, "DemoNote"), {
  title: string(),
  body: string(),
});

const DemoSection = bicodec.entity(defineType(NS, "DemoSection"), {
  title: string(),
  summary: string(),
  items: array(entityRef(DemoNote)), // or union with DemoSection for recursion
});
```

Where `NS` is a namespace uuid picked at random for this demo.

### Server (`src/server.ts`)

Runs on port 3460 (one more than fingerprinting's 3458/3459):

- Create a scope.
- Instantiate two `DemoNote` entities with placeholder copy ("Welcome
  note", "Second note").
- Instantiate one `DemoSection` whose `items` point at both notes.
- Pin: `root` → DemoSection id; `first-note` → the first DemoNote id.
- Serve via `createFxtrApp(scope, pins)` + `@hono/node-server`.

### Client (`src/client/main.tsx` + `App.tsx`)

`main.tsx` is thin: side-effect imports for CSS (dockview +
workstation theme), mounts `<App />`.

`App.tsx`:

1. On mount, connects to the server via `createHttpReadNamespace` at
   `http://localhost:3460`.
2. Retrieves the two pins.
3. Renders a tiny header with two buttons ("Show root",
   "Show first note") and `<WorkstationShell entity={selected} />`
   below.
4. Wires `<FxtrProvider scope={ns.scope}>` +
   `<DispatchProvider handler={...}>` around the shell.
5. Dispatch registry:
   - `DemoNote`: registers `FullPanel`, `BlockRef`, `HoverPreview`
     (skip `InlineRef` to exercise that fallback too).
   - `DemoSection`: registers **nothing** — so opening it in a panel
     shows the `JsonTree` fallback, with the `items` array rendering
     child `DemoNote` IDs as inline refs via their registered
     `InlineRef` fallback. (The "one entity intentionally
     unregistered" idea.)

### Scripts

In `example-dev-workstation/package.json`:

```json
{
  "scripts": {
    "serve": "tsx src/server.ts",
    "dev": "vite",
    "build": "vite build"
  }
}
```

Expected workflow: terminal 1 `pnpm --filter @fxtr-workstation/example-dev serve`, terminal 2 `pnpm --filter @fxtr-workstation/example-dev dev`.

## Implementation todos

Roughly in dependency order:

- [x] Add `example-dev-workstation` scaffolding (package.json,
      tsconfig.json, vite.config.ts, index.html, empty src stubs) and
      register in `tsconfig.json`.
- [x] `src/shortId.ts` — one-liner helper.
- [x] `src/keys.ts` — declare the four dispatch keys.
- [x] `src/components/JsonTree.tsx` — port + adapt.
- [x] `src/fallbacks/*` — four fallback components.
- [x] `src/registry.ts` — `createWorkstationDispatchHandler`. Verify
      `unknownLoadCodec` behavior; add shim if needed. (No shim needed —
      `unknownLoadCodec.supportsEntityType` already returns `Eager(true)`.)
- [x] `src/EntityPanelWrapper.tsx` — dockview IContentRenderer wrapper
      with `setPanelTitle` plumbing.
- [x] `src/DockviewLayout.tsx` — DockviewReact container, panel
      registration, opens a single panel by entity id.
- [x] `src/WorkstationShell.tsx` — top-level component; syncs the
      single panel to the `entity` prop.
- [x] `src/index.ts` — re-export all of the above.
- [x] `example-dev-workstation/src/entities.ts` — two types.
- [x] `example-dev-workstation/src/server.ts` — Hono server, pins.
      (Mirrors fingerprinting-report's pattern: raw node:http delegating
      to a Hono `createFxtrApp` for fxtr routes and vite middleware for
      the client — no separate `dev` task needed.)
- [x] `example-dev-workstation/src/client/App.tsx` — namespace + shell.
- [x] `example-dev-workstation/src/client/main.tsx` — vite entry,
      CSS imports.
- [x] `example-dev-workstation/index.html`.
- [x] Run `pnpm install`, `pnpm check`, `pnpm lint` — all clean.
- [x] Start the server (`pnpm --filter @fxtr-workstation/example-dev serve`).
      Server + vite run from the same process; no separate `dev` start.
- [x] Smoke-tested via curl: `/pins`, `/pin/demo/section`,
      `/src/client/{main,App,handlers}.tsx` all return 200 with the
      expected payloads. **Visual verification (dockview chrome vs.
      theme, panel behavior) pending user.**
- [x] Update `projects/fxtr_workstation/ts/CODE_OVERVIEW.md` with the new
      packages' file layouts and the dev demo run instructions.

## Out of scope for step 3

- Interactions: drag, hover preview cards, right-click context menu,
  dockview drop handling. → step 4.
- Open-panels sidebar, namespace panel. → step 5.
- Breadcrumb callback. → step 6.
- `ReactNode` panel titles (custom tab component). → post-phase-0.
- Layout persistence to localStorage. → step 4 or 5 (probably 5 with
  the namespace panel's auto-reopen).
- Anything fingerprinting-specific. → step 7.

## Follow-ups surfaced during implementation

- **FxtrProvider under React StrictMode.** In dev with StrictMode,
  `FxtrProvider`'s effect-cleanup fires on the fake-unmount and
  `scheduleClose()`s the internal provider scope. The render-phase
  recreation block doesn't run again (no re-render is triggered), so
  child `<Render>` components using `useFxtrScope` end up trying to
  `createFxtrScope({ include: [closedProviderScope] })` and throw
  `Parent scope "fxtr-react-provider" has already been shut down`. The
  fingerprinting-report demo sidesteps it by not using StrictMode; we
  do the same in `example-dev-workstation/src/client/main.tsx` and
  leave a note there. Proper fix lives in fxtr-react: either keep the
  provider scope alive across a fast unmount→remount, or recreate it
  from the effect-setup path rather than only during render.
- **App useEffect never closes the namespace scope.** When/if the root
  unmounts for real, we leak the HTTP namespace scope. Not urgent for
  a dev demo, but a `ns.scope.scheduleClose()` in the cleanup would be
  correct. Skipped today because the cleanup fires during the initial
  in-flight init (before `ns` exists) and guarding adds noise we don't
  need yet.
- **JsonTree uses real `<button>`s for toggles.** Required by biome's
  `useKeyWithClickEvents` rule. Visually styled to match inline spans
  via a small `buttonReset` CSS object; fine, but worth knowing if the
  a11y tree shape matters later.
- **EntityPanelWrapper sync effect has no deps** — `api.setTitle()`
  fires after every render. Idempotent per dockview, cheap in practice;
  diff against a `lastSetRef` if it ever shows up in profiles.
- **WorkstationShell opens the initial panel in two places**
  (the ready handler and the entity-prop effect) with `api.getPanel(id)`
  guards. Awkward but correct; will dissolve naturally once step 5
  introduces a real "default layout" routine around the namespace
  panel.
- ~~**`DemoSection.items` uses `bicodec.fxtrId()` instead of
  `bicodec.entityRef(DemoNote)`.**~~ Resolved — switched to
  `array(entityRef(DemoNote))`. Wire format is identical either way
  (both encode as tagged `RawWeak/StrongFxtrID` values), but the typed
  codec gives future DemoSection handlers `EntityRef<DemoNote>[]`
  without re-constructing refs. Server passes the `EntityRef`s returned
  by `DemoNote.create(...).store(scope)` directly.
