# @fxtr/reports-react-handlers — Code Overview

Standard renderers for report entities. Bridges the gap between report data entities (`RootReport`, `SubReport`, `SimpleComment`, `LinkedSVGFigure`) and their React UI representations by providing handler components for each entity × dispatch-key combination.

## Architecture

The package uses the dispatch system from `@fxtr/react-dispatch`. Each entity type is registered with handlers for the dispatch keys it supports:

| Entity | MainView | FigureBlock | ReferenceLink | MarginNote | SummaryCard |
|--------|----------|-------------|---------------|------------|-------------|
| RootReport | x | | x | | x |
| SubReport | x | x | x | | |
| SimpleComment | x | | x | x | |
| LinkedSVGFigure | x | x | x | | |

`createReportDispatchHandler()` assembles all entries into a dispatch registry ready for use with `<DispatchProvider>`.

## Files

```
src/
├── index.ts                    # Public API exports
├── registry.ts                 # createReportDispatchHandler() — assembles all entries
├── treeRenderer.tsx            # ReportTreeRenderer: FxtrCbor tuple tree → React elements
└── handlers/
    ├── RootReport.tsx          # RootReportMainView, RootReportReferenceLink, RootReportSummaryCard
    ├── SubReport.tsx           # SubReportMainView, SubReportFigureBlock, SubReportReferenceLink
    ├── SimpleComment.tsx       # SimpleCommentMarginNote, SimpleCommentMainView, SimpleCommentReferenceLink
    ├── LinkedSVGFigure.tsx     # LinkedSVGFigureMainView, LinkedSVGFigureFigureBlock, LinkedSVGFigureReferenceLink
    └── GenericFallback.tsx     # Catch-all placeholder for unregistered entity types
tests/
└── treeRenderer.test.tsx       # isNodeTuple, preview target tests
```

- **treeRenderer.tsx**: `<ReportTreeRenderer>` recursively walks FxtrCbor tuple arrays `[type, props, children]` and produces React elements. Recognizes HTML elements, SVG elements (with interactive preview targets via `data-preview-target`), custom component nodes (`RenderFigure`, `RenderLink`), and fragments (`__fragment`).
- **handlers/**: Each file exports handler components that receive an `EntityRef<T>` and render the entity for a specific dispatch key. Handlers use `useFxtrLoad` to load entity data and `ReportTreeRenderer` to render component tree fields. `RootReportMainView` also handles annotation resolution and margin note attachment.
- **registry.ts**: `createReportDispatchHandler()` calls `createDispatchRegistry()` with all entity×key entries and a `GenericFallback` as the default.

## Dependencies

- `@fxtr/core` — FxtrID, EntityRef, FxtrCbor
- `@fxtr/react` — useFxtrLoad
- `@fxtr/react-dispatch` — Render, createDispatchRegistry, entry
- `@fxtr/reports-entities` — RootReport, SubReport, SimpleComment, LinkedSVGFigure
- `@fxtr/reports-react` — useReportNavigation, AttachMarginNote, dispatch keys
- `react`, `react-dom` (peer, >=18)
