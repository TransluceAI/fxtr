# @fxtr/reports-react — Code Overview

Report rendering framework providing the shell, navigation, dispatch keys, and layout primitives for displaying reports. Does not include the actual entity renderers (those are in `@fxtr/reports-react-handlers`).

## Key Concepts

- **Dispatch keys**: Named rendering slots (`MainView`, `FigureBlock`, `ReferenceLink`, `MarginNote`, `SummaryCard`) that `@fxtr/react-dispatch` uses to select the right component for a given entity type.
- **Navigation**: Breadcrumb-based navigation state with reducer pattern. Supports push, replace, back, and clear operations. Optionally syncs to browser URL.
- **ReportShell**: Top-level scaffold providing the CSS grid layout (breadcrumbs + main content + sidebar), view switching (cards vs main), and annotation resolution.

## Files

```
src/
├── index.ts           # Public API exports
├── keys.ts            # Dispatch keys: MainView, FigureBlock, ReferenceLink, MarginNote, SummaryCard
├── types.ts           # BreadcrumbEntry, ReportView, JsonValue
├── navigation.ts      # ReportNavigation context, useReportNavigation() hook, navigationReducer
├── marginNote.tsx     # AttachMarginNote — portal component for rendering into the sidebar
├── urlState.ts        # parseUrlState() / buildUrl() — URL serialization of navigation state
└── ReportShell.tsx    # Main scaffold: layout, breadcrumbs, view switching, annotation resolution, URL sync
tests/
└── navigation.test.ts # Navigation reducer unit tests
```

- **keys.ts**: Each key is created via `declareDispatchKey<Props>()` from `@fxtr/react-dispatch`. Props types define what extra data each rendering slot expects.
- **navigation.ts**: `useReportNavigation()` returns `{ navigateTo, navigateBack, breadcrumbs, current }`. The reducer manages a breadcrumb stack — new entities push, same entity replaces navState.
- **ReportShell.tsx** (~425 lines): Accepts `rootEntities`, optional `commentResolver`, and URL sync config. Manages two view modes: "cards" (grid of `SummaryCard` dispatches) and "main" (single entity with `MainView` dispatch + margin notes). Breadcrumbs appear at top; annotations render in a 280px right sidebar via portal.
- **urlState.ts**: Serializes entity ID to the URL path and navState/breadcrumbs to query params. `parseUrlState()` reconstructs navigation state from the current URL.
- **marginNote.tsx**: `<AttachMarginNote>` uses a React portal to render children into the sidebar container managed by `ReportShell`.

## Dependencies

- `@fxtr/core` — FxtrID
- `@fxtr/react` — useFxtrScope
- `@fxtr/react-dispatch` — declareDispatchKey, Render, DispatchProvider
- `react` (peer, >=18)
