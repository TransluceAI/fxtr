# `@fxtr-ui/reports`

Workstation-shaped renderers + a 5th dispatch key (`Figure`) for
displaying report entities inside the
[`@fxtr-ui/workstation`](../fxtr-ui-workstation/) panel shell.

The data model lives in the Python package `fxtr_report_builder`
(under `projects/fxtr_report_builder/`); this package consumes the
wire-decoded shape via `EntityRefStub`s and the standard load /
metadata machinery.

## Mental model

**Two report entity types, both Python-owned:**

- `RootReport` — top-level report; renders as a workstation
  `FullPanel`. Body is an MDX-compiled FxtrCbor tree (read as
  `RootReportValue.contents` on the frontend).
- `SubReport` — a section: small enough to embed as a
  `Figure` inside another report's body, also expandable to a
  `FullPanel` view in its own panel.

**`Figure` is a new dispatch key.** Modeled after the existing
four workstation keys, with one Props field: `ownStorage:
FxtrSessionAtom<unknown>` — same convention as `FullPanel`. The
containing report's `FullPanel` renderer projects per-figure
sub-atoms out of its own `ownStorage` (keyed by the
MDX-builder-assigned `figureKey` string).

**Default no-match fallback re-dispatches to `FullPanel`.** When
the dispatcher can't find a registered `Figure` renderer for an
entity type, `FigureNoMatchFallback` kicks in: wraps the
entity's `FullPanel` in a shaded container with a `RefLink`
header. So every type "just works" as a figure even without
explicit configuration — the embed still shows the entity body
inline, framed as a figure.

**Tree renderer walks `[type, props, children]` tuples.** HTML
element names → `createElement`; `RenderFigure` props.entity →
`<RenderDispatch as={Figure} />`; `RenderLink` props.entity →
`<RefLink as="inline" />`; `__fragment` flattens children;
unknown element names → dashed-border placeholder. SVG element
names accepted alongside HTML.

**Figure keys are assigned at MDX-compile time.** The
`fxtr-report-builder-cli` flatten pass walks the tree, gives
each `RenderFigure` node an incrementing
`figureKey: "figure-<i>"` baked into its props. The frontend
threads that key through `figureStorage(figureKey)` to derive a
stable per-figure storage slot scoped to the panel session.
Stable across re-mounts; figures referencing the same entity
twice get distinct slots; reordering MDX figures shifts the
keys (resets prior state — acceptable trade-off).

**No `RootReport`-as-figure renderer.** Embedding a `RootReport`
inside another report's body falls through to
`FigureNoMatchFallback` (the entity's `FullPanel` framed as a
figure). Consumers wanting nested figures should reach for
`SubReport`.

## File layout

```
fxtr-ui-reports/
├── LOCAL_CODE_OVERVIEW.md
├── package.json
├── tsconfig.json
└── src/
    ├── index.ts            # public re-exports
    ├── keys.ts             # Figure key + FigureProps
    ├── fallbacks.tsx       # FigureNoMatchFallback (shaded FullPanel + RefLink)
    │                       # plus FigureLoadingFallback / FigureErrorFallback
    ├── figure_storage.ts   # makeFigureStorage(figuresAtom) → key-keyed sub-atom lookup
    ├── tree_renderer.tsx   # ReportTreeRenderer — walks the wire-decoded tree
    ├── types.ts            # RootReportValue, SubReportValue wire shapes
    └── handlers/
        ├── shared.tsx      # CSS style tokens shared across renderers
        ├── RootReport.tsx  # FullPanel / BlockLink / InlineLink / HoverPreview
        └── SubReport.tsx   # all 5 keys including Figure (figure-mode rendering)
tests/
└── figure_storage.test.ts  # makeFigureStorage lookup behavior
```

React-render-layer tests (tree renderer dispatching, handler
behavior) are intentionally deferred to the demo's hand-test
loop (per the workspace convention of putting jsdom-shaped
tests in sibling `*-tests` packages — see
[`fxtr-ui-react-tests/`](../fxtr-ui-react-tests/)). The
framework-free `figure_storage.test.ts` covers the only piece of
plain-data logic in the package.

## Module details

### `keys.ts`

Declares `Figure` via `declareDispatchKey<FigureProps>`. Hardcoded
UUID. Bundle of three fallback components imported from
`fallbacks.tsx`. Both entity and service stubs allowed.

### `fallbacks.tsx`

Three components, all themed via the workstation CSS tokens:

- `FigureNoMatchFallback`: shaded wrapper +
  `<RefLink stub={stub} as="inline">` header above an inner
  `<RenderDispatch as={FullPanel} stub={stub} ownStorage={...}/>`.
  Threads through the same `ownStorage` the figure key was
  given, so the inner FullPanel reuses the figure's storage slot.
  The body has a `max-height` with `overflow: auto` and
  `resize: vertical` so the nested FullPanel never blows up the
  figure's height and the user can drag the bottom-right corner
  to grow it when needed.
- `FigureLoadingFallback`: simple "loading figure…" skeleton.
- `FigureErrorFallback`: error pill with the stub's shortId +
  the caught error message.

### `figure_storage.ts`

`makeFigureStorage(figuresAtom)` builds a lookup `(figureKey) →
FxtrSessionAtom<unknown>`. The returned function memoizes
per-key so multiple calls return the same atom instance —
identity-stable for `useMemo`-keyed React subtrees.
Implementation: thin wrapper around `subAtom` from
`@fxtr-ui/state-util`.

### `tree_renderer.tsx`

`<ReportTreeRenderer document figureStorage>` walks each tuple,
dispatches on the `type` string. The element-name allowlists
(HTML + SVG) are copied verbatim from the older
`@fxtr/reports-react-handlers/treeRenderer.tsx`. `RenderFigure`
nodes pass through to `<RenderDispatch as={Figure} ...>` with
the projected `ownStorage` sub-atom; `RenderLink` nodes become
`<RefLink as="inline">`.

### `makeLoadResultAtom` / `LoadResult<T>` (re-exported)

These used to live here in `load_effect.ts`; they were lifted
to `@fxtr-ui/state-util` so other packages can use them without
taking a dep on `@fxtr-ui/reports`. The reports package
re-exports them from its `index.ts` for back-compat. See
`packages/fxtr-ui-state-util/src/load_result_atom.ts` for the
implementation. The canonical "load + project to 3-arm union"
helper used by every renderer below.

### `handlers/` — the canonical renderer pattern

Every renderer in the package follows the same shape:

1. Get the atom(s) the renderer's session state lives in —
   either narrowed from `ownStorage` (FullPanel / Figure
   renderers) or via `useDispatchSharedAtom` (link / hover
   keys).
2. Inside `useMemo`, declare the renderer-specific state
   interface, project the slot atoms it needs (via `subAtom`),
   build a `LoadResult<T>` atom via `makeLoadResultAtom`, and
   (where the renderer needs figures) a figure-storage closure
   via `makeFigureStorage`. Return them.
3. `useAtomValue` the result atom and (if applicable) any
   derived/storage closures.
4. Render: switch on `result.kind` for distinct UI per state,
   or just fall back to a placeholder if there's nothing
   kind-specific to render.

`RootReport.tsx`: 4 renderers. `FullPanel` narrows `ownStorage`
to `{load, figures}`. `BlockLink` / `InlineLink` /
`HoverPreview` use the dispatcher session's shared atom
(`useDispatchSharedAtom`) directly as the load slot — these
only need the report's title / summary strings, no figure
storage needed.

`SubReport.tsx`: 5 renderers including `Figure`. `FullPanel`
narrows to `{load, overviewFigures, detailFigures}` — the
overview and detail bodies each get their own figure-storage
namespace. `Figure` (figure-mode) narrows to `{load,
overviewFigures}`. `HoverPreview` uses the dispatcher session
with `{load, overviewFigures}` and renders title + the
overview tree (with nested figures rendering through the same
machinery — they get their own per-figure storage slots
projected from the hover's shared atom). `BlockLink` /
`InlineLink` use the dispatcher session with the raw
`OpResultStub | null` shape since they only need the label.

`shared.tsx`: CSS style tokens used by both files. No reactive
logic.

## How to register the report renderers

Consumers compose their own dispatch registry — there's no
package-level helper. Pseudo-shape:

```ts
import { Figure, RootReportFullPanel, SubReportFigure, /* ... */ } from "@fxtr-ui/reports";
import { FullPanel, BlockLink, /* ... */ } from "@fxtr-ui/workstation";
import { entityEntry, staticEntityTypeDispatch, createDispatchRegistry } from "@fxtr-ui/react-dispatch";

const registry = createDispatchRegistry({
  entityEntries: [
    entityEntry(FullPanel, staticEntityTypeDispatch({ lookupMap: new Map([
      [rootReportTypeId, RootReportFullPanel],
      [subReportTypeId, SubReportFullPanel],
      /* other types */
    ])})),
    entityEntry(Figure, staticEntityTypeDispatch({ lookupMap: new Map([
      [subReportTypeId, SubReportFigure],
      /* leave un-figure-rendered types empty — they fall through
         to FigureNoMatchFallback (shaded FullPanel + reflink) */
    ])})),
    /* other keys: BlockLink, InlineLink, HoverPreview */
  ],
  serviceEntries: [/* ... */],
});
```

See the `@fxtr-ui/workstation-demo` package for the canonical
wiring (lands in step 14 of stage1_report_views).

## Dependencies

- `@fxtr-ui/core` — abstract stubs, `FxtrSessionAtom`
- `@fxtr-ui/react` — React layer integration
- `@fxtr-ui/react-dispatch` — `RenderDispatch`,
  `useDispatchSharedAtom`, `declareDispatchKey`
- `@fxtr-ui/state-util` — `atomWithDefault`, `subAtom`
- `@fxtr-ui/workstation` — `FullPanel`, `RefLink`, the workstation
  Props types (BlockLink / InlineLink / HoverPreview)
- `jotai`, `jotai-effect`, `react` (peer)

The dependency on `@fxtr-ui/workstation` is a known layering —
the package builds on top of the workstation shell rather than
beside it. A future refactor may split this, see the plan in
`projects/fxtr_workstation/plans/phase0/part4_assembly/stage1_report_views.md`.
