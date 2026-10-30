# @fxtr/example-demo-report — Code Overview

Demo app showing the full reports pipeline: server constructs a rich entity graph of reports, sub-reports, SVG figures, and comments; client renders everything with `ReportShell`.

## Server

Constructs entities at startup using `evaluateMdx()` for runtime MDX compilation:

- **SubReports**: Category breakdowns (Category A, B, C details)
- **LinkedSVGFigure**: An SVG bar chart with clickable bars linking to SubReport entities
- **RootReports**: Two reports — "Q3 2025 Market Analysis" (with SVG figure, methodology sub-report, cross-links) and "Q3 2025 Operational Metrics" (linked from Report 1)
- **SimpleComments**: Three comments targeting various entities

Custom MDX components (`RenderFigure`, `RenderLink`) emit dispatch nodes consumed by the client-side tree renderer. Entity references passed via `RawWeakFxtrID` props.

Serves fxtr HTTP on port 3457.

## Client

- Uses `DispatchProvider` with `createReportDispatchHandler()` from `@fxtr/reports-react-handlers`.
- Discovers reports and comments via pins using `listPins()` and `retrieve()`.
- Implements a comment resolver that loads comments and matches them to their targets.
- Renders reports in `ReportShell` with card list view, main view with breadcrumbs, margin notes, and SVG drill-down.
- Supports URL synchronization for navigation state.

## Files

```
src/
├── server.ts             # Entity graph construction, fxtr HTTP server
└── client/
    ├── main.tsx          # React entry point
    └── App.tsx           # Provider setup, pin discovery, annotation filtering, ReportShell
vite.config.ts
index.html
```

## Running

```bash
pnpm dev    # starts server on port 3457
```

## Dependencies

- `@fxtr/core`, `@fxtr/http-server`, `@fxtr/http-client`, `@fxtr/react`
- `@fxtr/mdx-transform`, `@fxtr/react-dispatch`
- `@fxtr/reports-entities`, `@fxtr/reports-react`, `@fxtr/reports-react-handlers`
