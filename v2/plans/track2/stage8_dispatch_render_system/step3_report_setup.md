# Report System Setup — Implementation Plan

This plan covers the "Initial setup for report system" section of `overview_sketch.md`: the report dispatch keys, entity data model, standard renderers, and a demo app.

See `report_setup_scratch.md` for design working notes.

## Package Structure

Four new packages in `v2/ts/packages/`:

```
@fxtr/reports-entities       (packages/fxtr-reports-entities/)
@fxtr/reports-react          (packages/fxtr-reports-react/)
@fxtr/reports-react-handlers (packages/fxtr-reports-react-handlers/)
@fxtr/example-demo-report    (packages/example-demo-report/)
```

Dependency graph:

```
@fxtr/reports-entities          ← only @fxtr/core
       ↓
@fxtr/reports-react             ← @fxtr/core, @fxtr/react, @fxtr/react-dispatch
       ↓
@fxtr/reports-react-handlers    ← all three above
       ↓
@fxtr/example-demo-report       ← all above + @fxtr/mdx-transform, @fxtr/http-server, vite
```

---

## 1. @fxtr/reports-entities — Data Model

Pure entity definitions. No React, no rendering. Only depends on `@fxtr/core`.

### Entities

**RootReport** (`defineType("fxtr-reports", "RootReport")`):
```ts
{
  title: bicodec.string(),       // used in reference link, breadcrumbs
  summary: bicodec.string(),     // used in summary card
  contents: bicodec.array(bicodec.unknown()),  // MDX component tree fragment
}
```

**SubReport** (`defineType("fxtr-reports", "SubReport")`):
```ts
{
  label: bicodec.string(),       // short label for reference links / breadcrumbs
  overview: bicodec.array(bicodec.unknown()),  // shown in figure mode
  detail: bicodec.array(bicodec.unknown()),    // additional content in main view
}
```

(Adding `label` for reference link text; the overview doesn't mention it explicitly but every entity that supports ReferenceLink dispatch needs some textual identifier.)

**SimpleComment** (`defineType("fxtr-reports", "SimpleComment")`):
```ts
{
  author: bicodec.string(),
  comment: bicodec.string(),
  target: bicodec.fxtrId(),      // weak ref to the commented-on entity
}
```

**LinkedSVGFigure** (`defineType("fxtr-reports", "LinkedSVGFigure")`):
```ts
{
  label: bicodec.string(),                     // caption / reference text
  svg: bicodec.unknown(),                      // single FxtrCbor node: ["svg", {...}, [...]]
  links: bicodec.record(bicodec.fxtrId()),     // string key → FxtrID (weak)
}
```

`svg` is a single component tree node using SVG element types (svg, rect, circle, path, etc.). The tree renderer handles these as native SVG elements. `links` maps `data-preview-target` attribute values to entity IDs for interactive drill-down.

### Files

```
packages/fxtr-reports-entities/
  package.json
  tsconfig.json
  src/
    index.ts        # re-exports all entities
    entities.ts     # RootReport, SubReport, SimpleComment, LinkedSVGFigure
```

### Implementation steps

- [x] Create package skeleton (package.json, tsconfig.json)
- [x] Wire into workspace (root tsconfig.json references, pnpm-workspace already covers packages/*)
- [x] Implement entity definitions in `entities.ts`
- [x] Write `index.ts` exports
- [x] Add basic tests (round-trip store/load for each entity)

---

## 2. @fxtr/reports-react — Report Framework

Dispatch keys, navigation context, annotation resolution, margin note portal, and scaffold layout. This is the "report framework" that the handlers and demo app build on top of.

### Dispatch keys

Five keys, declared with `declareDispatchKey`:

```ts
const MainView = declareDispatchKey<{
  navState?: FxtrCbor;
  annotations?: FxtrID[];
}>("reports:MainView");

const FigureBlock = declareDispatchKey<{}>("reports:FigureBlock");

const ReferenceLink = declareDispatchKey<{
  linkText?: string;
}>("reports:ReferenceLink");

const MarginNote = declareDispatchKey<{
  highlighted?: boolean;
}>("reports:MarginNote");

const SummaryCard = declareDispatchKey<{}>("reports:SummaryCard");
```

Exported from `src/keys.ts`.

### Navigation context

A React context providing navigation within the report system. Managed by the scaffold.

```ts
type BreadcrumbEntry = { entityId: FxtrID; navState?: FxtrCbor };

interface ReportNavigation {
  navigateTo(entityId: FxtrID, navState?: FxtrCbor): void;
  navigateBack(index: number): void;
  breadcrumbs: readonly BreadcrumbEntry[];  // entries[last] is current
  currentEntry: BreadcrumbEntry | null;
}
```

Navigation rules:
- `navigateTo` with a new entityId → push to breadcrumb stack
- `navigateTo` with the same entityId but different navState → replace top entry (no new breadcrumb)
- `navigateBack(i)` → pop to index i, discard everything after

Hook: `useReportNavigation()` returns the `ReportNavigation` object. Throws if called outside a scaffold.

### Annotation resolution

The scaffold accepts an `annotationsFor` prop:

```ts
annotationsFor?: (entityId: FxtrID) => PromiseOrEager<FxtrID[]>
```

When the current main view entity changes (due to navigation), the scaffold calls this function and passes the resulting annotation IDs to `<Render ... as={MainView} annotations={annotationIds}>`. The demo app implements the filtering logic (e.g. matching SimpleComment.target to the current entity ID).

Internally, the scaffold stores the resolved annotations in state. When `annotationsFor` returns a Promise, annotations may briefly be empty until the promise resolves (the main view renders without annotations first, then re-renders with them). When it returns Eager, annotations are available immediately.

### Margin note portal

`<AttachMarginNote>` wraps arbitrary content and portals it to the sidebar region of the scaffold layout.

Implementation:
- Scaffold renders a `<div>` as the sidebar target, stores its ref in context
- `useMarginNoteTarget()` internal hook reads the ref from context
- `<AttachMarginNote>` calls `createPortal(children, target)` to render into the sidebar
- If no target exists (outside a scaffold), children render inline as a fallback

### Scaffold component — `<ReportShell>`

The top-level layout component that wires everything together.

```tsx
type ReportView =
  | { kind: "cards"; entities: FxtrID[] }
  | { kind: "main"; rootEntity: FxtrID };

<ReportShell
  initialView={view}
  annotationsFor={...}
/>
```

The shell:
1. Renders breadcrumbs above the main area (only in main view mode)
2. When in "cards" mode: renders each entity as `<Render entity={e} as={SummaryCard} />` in a grid, full width (no sidebar)
3. When in "main" mode: renders `<Render entity={e} as={MainView} navState={navState} annotations={annotations} />` in a two-column layout (main + sidebar for margin notes)
4. Navigating from a card (via `navigateTo`) transitions to main view mode and pushes a breadcrumb
5. Breadcrumbs render as clickable items using `<Render entity={e} as={ReferenceLink} />` (each is clickable via `navigateBack`)

CSS Grid layout:
```css
.report-shell {
  display: grid;
  grid-template-columns: 1fr 280px;
  grid-template-rows: auto 1fr;
  grid-template-areas:
    "breadcrumbs breadcrumbs"
    "main        sidebar";
}
```

In card mode, sidebar column collapses (or single column layout).

The shell must be inside a `<FxtrProvider>` and `<DispatchProvider>` — it does not create these itself (the app provides them). The shell provides the `ReportNavigationContext` (for `useReportNavigation`) and `MarginNoteTargetContext` (for `<AttachMarginNote>`).

### Files

```
packages/fxtr-reports-react/
  package.json
  tsconfig.json
  src/
    index.ts              # public exports
    keys.ts               # dispatch key declarations
    navigation.ts         # BreadcrumbEntry, ReportNavigation types, useReportNavigation, context
    marginNote.tsx        # AttachMarginNote component, context
    ReportShell.tsx        # scaffold component, layout, state management
    types.ts              # ReportView and other shared types
    shell.css             # layout styles
```

### Implementation steps

- [x] Create package skeleton
- [x] Wire into workspace
- [x] `keys.ts`: declare all five dispatch keys
- [x] `types.ts`: ReportView, BreadcrumbEntry
- [x] `navigation.ts`: navigation context, `useReportNavigation` hook, navigation state management (reducer)
- [x] `marginNote.tsx`: `AttachMarginNote`, portal target context
- [x] `ReportShell.tsx`: scaffold component with layout, breadcrumbs, annotation resolution, card/main view switching
- [x] Using inline styles instead of separate CSS file
- [x] `index.ts`: public exports
- [x] Tests for navigation state logic (push, replace, back)
- [x] Update CODE_OVERVIEW.md

---

## 3. @fxtr/reports-react-handlers — Standard Renderers

Dispatch handler implementations for each entity type × dispatch key, plus a component tree renderer.

### Component tree renderer

A `<ReportTreeRenderer document={fxtrCborArray} />` component that walks FxtrCbor tuple trees and maps them to React elements. Similar to the example-mdx-rendering renderer but with report-specific custom components.

**Element type handling:**
- HTML elements (div, p, h1, etc.) → native React elements
- SVG elements (svg, rect, circle, path, g, text, line, etc.) → native SVG elements
- `RenderFigure` custom component → `<Render entity={props.entity} as={FigureBlock} />`
- `RenderLink` custom component → `<Render entity={props.entity} as={ReferenceLink} linkText={props.linkText} />`
- `__fragment` → unwrap children inline
- Unknown → placeholder div

The entity props in `RenderFigure` / `RenderLink` arrive as `RawWeakFxtrID` values in the FxtrCbor tree (the MDX transform embeds weak refs). The renderer extracts the inner `FxtrID` from these.

### SVG interaction layer

For LinkedSVGFigure renderers, SVG elements with `data-preview-target` attributes need click handlers. The tree renderer accepts an optional `onPreviewTargetClick?: (targetKey: string) => void` callback. When provided, elements with `data-preview-target` get an onClick that calls this with the attribute value.

The LinkedSVGFigure handler components wire this up to `navigateTo(thisFigureId, targetKey)`.

### Handler implementations

Each handler is a React component receiving `entityRef: EntityRef<T>` (via the registry's wrapping).

**RootReport:**
- **MainView**: Load entity via `useFxtrLoad`. Render annotations as `<Render as={MarginNote}>` (portaled to sidebar via `<AttachMarginNote>`). Render `contents` via `<ReportTreeRenderer>`. Title as heading.
- **ReferenceLink**: Load entity. Render title as a clickable link (calls `navigateTo`).
- **SummaryCard**: Load entity. Show title + summary in a card. Clickable → `navigateTo`.

**SubReport:**
- **MainView**: Load entity. Render both `overview` and `detail` via `<ReportTreeRenderer>`. Label as heading.
- **FigureBlock**: Load entity. Render only `overview` via `<ReportTreeRenderer>`. Include a "Show details" link that calls `navigateTo` to focus this subreport as main view.
- **ReferenceLink**: Load entity. Render label as a clickable link.

**SimpleComment:**
- **MarginNote**: Load entity. Show author and comment in a compact card. If `highlighted` prop, add visual emphasis.
- **MainView**: Load entity. Show author + comment, plus a reference link to the target entity above it (`<Render entity={target} as={ReferenceLink} />`).
- **ReferenceLink**: Load entity. Show "Comment by {author}" as clickable link.

**LinkedSVGFigure:**
- **MainView**: Load entity. Render SVG via tree renderer with `onPreviewTargetClick` wired to set navState. If navState matches a key in `links`, render the linked entity as `<Render entity={linkedId} as={FigureBlock} />` below, labeled "Selected:".
- **FigureBlock**: Load entity. Render SVG via tree renderer with `onPreviewTargetClick` wired to navigate to this figure as main view with the target as navState.
- **ReferenceLink**: Load entity. Render label as clickable link.

### Registry assembly

Export a helper that builds the full `DispatchHandler`:

```ts
export function createReportDispatchHandler(): DispatchHandler {
  return createDispatchRegistry([
    entry(MainView, RootReport, RootReportMainView),
    entry(ReferenceLink, RootReport, RootReportReferenceLink),
    entry(SummaryCard, RootReport, RootReportSummaryCard),
    entry(MainView, SubReport, SubReportMainView),
    entry(FigureBlock, SubReport, SubReportFigureBlock),
    entry(ReferenceLink, SubReport, SubReportReferenceLink),
    entry(MarginNote, SimpleComment, SimpleCommentMarginNote),
    entry(MainView, SimpleComment, SimpleCommentMainView),
    entry(ReferenceLink, SimpleComment, SimpleCommentReferenceLink),
    entry(MainView, LinkedSVGFigure, LinkedSVGFigureMainView),
    entry(FigureBlock, LinkedSVGFigure, LinkedSVGFigureFigureBlock),
    entry(ReferenceLink, LinkedSVGFigure, LinkedSVGFigureReferenceLink),
  ], {
    fallback: GenericFallback,
  });
}
```

### Files

```
packages/fxtr-reports-react-handlers/
  package.json
  tsconfig.json
  src/
    index.ts                    # public exports
    treeRenderer.tsx            # ReportTreeRenderer component
    handlers/
      RootReport.tsx            # MainView, ReferenceLink, SummaryCard
      SubReport.tsx             # MainView, FigureBlock, ReferenceLink
      SimpleComment.tsx         # MarginNote, MainView, ReferenceLink
      LinkedSVGFigure.tsx       # MainView, FigureBlock, ReferenceLink
      GenericFallback.tsx       # catch-all for unhandled dispatch
    registry.ts                 # createReportDispatchHandler()
    handlers.css                # styles for handler components
```

### Implementation steps

- [x] Create package skeleton
- [x] Wire into workspace
- [x] `treeRenderer.tsx`: build the tree renderer with HTML, SVG, RenderFigure, RenderLink, and onPreviewTargetClick support
- [x] `GenericFallback.tsx`: simple fallback component
- [x] `RootReport.tsx`: MainView, ReferenceLink, SummaryCard handlers
- [x] `SubReport.tsx`: MainView, FigureBlock, ReferenceLink handlers
- [x] `SimpleComment.tsx`: MarginNote, MainView, ReferenceLink handlers
- [x] `LinkedSVGFigure.tsx`: MainView, FigureBlock, ReferenceLink handlers
- [x] `registry.ts`: createReportDispatchHandler
- [x] Using inline styles instead of separate CSS file
- [x] `index.ts`: public exports
- [x] Tests for tree renderer (isNodeTuple, preview target extraction)
- [x] Update CODE_OVERVIEW.md

---

## 4. @fxtr/example-demo-report — Demo App

A runnable demo that constructs report data and serves a frontend.

### Server side

Uses `@fxtr/mdx-transform`'s `evaluateMdx()` to dynamically compile MDX content into component tree fragments, stores them as report entities, and serves via `@fxtr/http-server`.

**Report 1 — full-featured report:**
- A RootReport with MDX content that includes:
  - Prose paragraphs
  - A SubReport embedded as a `<RenderFigure>` (figure block)
  - A LinkedSVGFigure embedded as a `<RenderFigure>`
  - `<RenderLink>` references to the SubReport and to the second report
- The SubReport has overview (brief) and detail (expanded) content
- The LinkedSVGFigure has a simple SVG (e.g. bar chart with three labeled bars) with `data-preview-target` on each bar, each linking to one of three small sub-reports that exist only to be linked from this SVG figure (e.g. "Category A Details", "Category B Details", "Category C Details" — each with a brief overview and a bit more detail)

**Report 2 — simple report:**
- A RootReport with simpler MDX content
- Includes a `<RenderLink>` reference back to Report 1

**Comments:**
- One SimpleComment targeting Report 1
- One SimpleComment targeting Report 2
- One SimpleComment targeting the SubReport from Report 1

**Pins:**
- `reports/report1` → Report 1
- `reports/report2` → Report 2
- `comments/1`, `comments/2`, `comments/3` → the three comments

Server runs via Hono + `@hono/node-server` (following the existing example patterns).

### Client side

A Vite-served React app:

1. Creates an `HttpFxtrReadNamespace` pointing at the server
2. Fetches pin list to discover all reports and comments
3. Sets up `<FxtrProvider>`, `<DispatchProvider handler={createReportDispatchHandler()}>`, then `<ReportShell>`
4. Initial view: card list showing all reports
5. Comments are wired into `annotationsFor`: filters by matching SimpleComment.target to the requested entity ID
6. User clicks a report card → navigates to main view → breadcrumbs appear → annotations show in sidebar → figures render inline → clicking SVG targets drills down → etc.

### Files

```
packages/example-demo-report/
  package.json
  tsconfig.json
  vite.config.ts
  index.html
  src/
    server.ts              # data construction + HTTP server
    data/
      report1.mdx          # or inline string if easier
      report2.mdx
    client/
      main.tsx             # React entry point
      App.tsx              # sets up providers, fetches data, renders shell
```

### Implementation steps

- [x] Create package skeleton with vite config
- [x] Wire into workspace
- [x] `server.ts`: construct all entities (reports, subreport, comments, SVG figure), serve with createFxtrApp
- [x] `client/main.tsx`: React entry, createRoot
- [x] `client/App.tsx`: provider setup, pin fetching, annotation filtering, ReportShell
- [x] `index.html`: minimal HTML shell
- [ ] Manual testing: verify all dispatch paths work (cards, main view, navigation, breadcrumbs, annotations, figures, SVG interaction)
- [x] Update CODE_OVERVIEW.md

---

## Implementation Order

The packages have strict dependency ordering, so implementation proceeds bottom-up:

1. **@fxtr/reports-entities** — pure data, no UI concerns
2. **@fxtr/reports-react** — framework layer (keys, navigation, shell)
3. **@fxtr/reports-react-handlers** — renderers + tree renderer + registry
4. **@fxtr/example-demo-report** — integration + demo

Within each package, the implementation steps above are ordered for incremental progress.

## Things to Revisit Later

- **Annotation timing**: When `annotationsFor` returns a Promise, the main view briefly renders without annotations then re-renders. May want a loading state or delay. Can iterate on this.
- **View transitions**: The overview mentions the View Transitions API. Leaving this for a future pass (listed as a future extension).
- ~~**URL integration**: Breadcrumbs are React-only state for now. Future extension: pushState/replaceState.~~ Done: `syncUrl` prop on ReportShell. Entity ID in URL path (`/<idString>`), navState + breadcrumbs in query params. pushState for new navigation, replaceState for navState-only changes, popstate listener for browser back/forward.
- **Styling**: Starting with real CSS Grid layout but keeping visual design minimal. Can iterate on aesthetics later. Using inline styles for library packages for now; consider CSS modules or similar in the future.
