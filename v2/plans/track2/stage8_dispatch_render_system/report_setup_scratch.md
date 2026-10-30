# Report Setup — Scratch Notes

Working through the design details before writing the final plan.

## Package dependency graph

```
@fxtr/reports-entities  (no report deps, only @fxtr/core)
       ↓
@fxtr/reports-react     (depends on @fxtr/core, @fxtr/react, @fxtr/react-dispatch)
       ↓
@fxtr/reports-react-handlers  (depends on all three above)
       ↓
@fxtr/example-demo-report     (depends on all above + @fxtr/mdx-transform, @fxtr/http-server)
```

## Entity schema details

**RootReport:**
- `title: string` — for reference link default text, breadcrumbs
- `summary: string` — for summary card
- `contents: FxtrCbor[]` — component tree fragment from MDX transform. Array of top-level nodes.

**SubReport:**
- `overview: FxtrCbor[]` — shown in figure mode
- `detail: FxtrCbor[]` — additional content shown when focused as main view (below overview)
- Needs a title for reference link / breadcrumb? The overview doesn't mention one explicitly. Could derive from content or add a short `label` field. I'll add a `label: string` field for reference link text.

**SimpleComment:**
- `author: string`
- `comment: string`
- `target: FxtrID` (weak ref — points to the thing this comments on)

**LinkedSVGFigure:**
- `svg: FxtrCbor` — a single component tree fragment node (the SVG element). Not an array; it's one `<svg>` node.
- `links: Record<string, FxtrID>` — maps string keys to entity IDs. SVG elements with `data-preview-target=key` link to these.
- Needs a caption/title? For reference link at least. Could add `label: string`.

## Dispatch key props

**MainView**: `{ navState?: FxtrCbor; annotations?: FxtrID[] }`
- navState is arbitrary JSON stored in navigation state
- annotations is the filtered list for this entity

**FigureBlock**: `{}` (empty — no extra props)

**ReferenceLink**: `{ linkText?: string }`

**MarginNote**: `{ highlighted?: boolean }`

**SummaryCard**: `{}` (empty)

## Navigation system

State:
```ts
type BreadcrumbEntry = { entityId: FxtrID; navState?: FxtrCbor };
// Stack: entries[0] is oldest, entries[entries.length-1] is current
```

`navigateTo(entityId, navState?)`:
- If entityId is different from current: push new entry
- If entityId is same but navState differs: replace top entry (no new breadcrumb)

`navigateBack(index)`:
- Pop back to entries[index], discarding everything after

Context: `useReportNavigation()` returns `{ navigateTo, currentEntry, breadcrumbs, navigateBack }`

## Scaffold component

```tsx
<ReportScaffold
  handler={dispatchHandler}  // DispatchHandler for dispatch
  // Two modes:
  // Mode 1: card list
  entities={[...]}  // list of FxtrIDs to show as cards
  // Mode 2: single entity main view
  rootEntity={fxtrId}
  annotationsFor={(entityId) => PromiseOrEager<FxtrID[]>}
>
```

Wait, the overview says the entry point "shows either one of two types of view". Maybe it should support showing both, or maybe the user toggles between them. Let me re-read:

> Shows either one of two type of view:
> - React component that takes a list of entities, and renders them in cards
> - React component that takes a single root entity and a list of annotations on it, and renders it as a main view

So these are two distinct rendering modes. The demo app decides which to show. Maybe the scaffold just provides the layout infrastructure (breadcrumbs, navigation, margin notes) and the demo app wires the two views together.

Actually, I think the cleanest approach:
- `<ReportShell>` provides layout (breadcrumbs bar, main area, margin notes sidebar), navigation context, and annotation resolution
- Within that, the demo app renders either a card list view or a main view
- But the main view rendering + breadcrumbs is tightly coupled, so maybe the shell handles both

Let me think about this differently. The shell component:
1. Takes initial content to show (either a list of cards, or a root entity)
2. When navigation happens (via useReportNavigation), it switches to showing the navigated-to entity as a main view
3. Breadcrumbs accumulate as you navigate
4. A "back to list" button could return to card view

So the shell's API might be:
```tsx
<ReportShell annotationsFor={...}>
  {/* Initial content — rendered when no navigation has happened */}
  <CardListView entities={[...]} />
</ReportShell>
```

And navigation from a card would push to the stack, replacing the initial content with the main view.

Actually, I think it's cleaner to just have the shell manage a view state:

```tsx
type ReportView =
  | { kind: "cards"; entities: FxtrID[] }
  | { kind: "main"; rootEntity: FxtrID; annotations: FxtrID[] };

<ReportShell
  initialView={view}
  annotationsFor={(entityId) => ...}
/>
```

When navigateTo is called, it pushes a new main view entry. The shell renders:
- If current view is "cards": render each entity as `<Render entity={e} as={SummaryCard} />`
- If current view is "main": render `<Render entity={e} as={MainView} navState={...} annotations={...} />`

Breadcrumbs show above the main view.

## Margin notes portal

React portal approach:
- Shell renders a `<div ref={marginNotesRef}>` as the sidebar
- Context provides the portal target ref
- `<AttachMarginNote>` reads the portal target from context and uses `createPortal` to render into it

## Tree renderer

Lives in @fxtr/reports-react-handlers. Takes a component tree fragment (FxtrCbor[]) and renders it. Custom component mappings:
- `RenderFigure` → `<Render entity={entityId} as={FigureBlock} />`
- `RenderLink` → `<Render entity={entityId} as={ReferenceLink} linkText={...} />`
- HTML elements → native elements
- SVG elements → native SVG elements (for LinkedSVGFigure rendering)

The tree renderer needs to handle both HTML and SVG element types.

## SVG interaction

For LinkedSVGFigure, elements with `data-preview-target` get an onClick handler:
- In FigureBlock mode: navigate to this figure as main view with navState = the target key
- In MainView mode: update navState to the target key (replace, don't push)

Both use `useReportNavigation().navigateTo(thisFigureEntityId, targetKey)`.

Wait, but the figure's entityId needs to be accessible inside the SVG renderer. The handler component has the entityRef, so it can pass the entityId down through the tree renderer.

## CSS layout

CSS Grid:
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

For card list view, sidebar is hidden and main takes full width.
