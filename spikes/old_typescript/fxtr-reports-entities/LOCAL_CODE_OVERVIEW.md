# @fxtr/reports-entities — Code Overview

Pure data entity definitions for the reporting system. No rendering or React — just bicodec-based entity types for storage and serialization.

## Entities

All entities use the `"fxtr-reports"` namespace for type IDs.

| Entity | Fields | Purpose |
|--------|--------|---------|
| **RootReport** | `title`, `summary`, `contents` (unknown[]) | Top-level report with title, summary, and MDX component tree |
| **SubReport** | `label`, `overview` (unknown[]), `detail` (unknown[]) | Section within a report with overview and detail component trees |
| **SimpleComment** | `author`, `comment`, `target` (weak FxtrID) | Comment with author text and weak reference to target entity |
| **LinkedSVGFigure** | `label`, `svg` (unknown[]), `mappings` (record of weak FxtrIDs) | SVG diagram with label, SVG component tree, and entity-linked regions |

The `contents`, `overview`, `detail`, and `svg` fields use `unknown()` codecs to store FxtrCbor component trees (produced by `@fxtr/mdx-transform`).

## Files

```
src/
├── entities.ts       # All 4 entity definitions
└── index.ts          # Public exports (entities + type aliases)
tests/
└── entities.test.ts  # Round-trip store/load tests
```

## Usage

```typescript
import { RootReport, SubReport } from "@fxtr/reports-entities";

const report = RootReport.create({ title: "Q3 Analysis", summary: "...", contents: [...] });
const ref = await report.store(scope);
const loaded = await ref.load(scope);
```

## Dependencies

- `@fxtr/core` — bicodec, defineType, FxtrID types
