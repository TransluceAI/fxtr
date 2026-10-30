# Extra 4: Fingerprint Report Demo

Interactive drill-down report for mental health fingerprint judge results, built as an fxtr entity graph served via `@fxtr/http-server` with a React client using `ReportShell`.

## Data Sources

All read from `/Users/daniel/extgit/mh_tmp_fingerprint_running/`:

- `results/*.json` — judge results per transcript (~5500 files)
- `data/conversations/*.json` — raw conversations with transcripts
- `mh_judge_lib_pkg/mh_judge_lib/data/multi_fingerprint_specs.toml` — dimension/category definitions with rubrics
- `data/user_simulators_uwbot.json` — simulator bios and steering instructions
- `data/roster_config.json` — which sims are in scope

## Entity Hierarchy & Navigation Flow

```
RootReport (top-level overview in MDX)
├── RenderFigure → LinkedSVGFigure (category prevalence bar chart)
│   └── each bar data-preview-target → CategoryAggregate
│       ├── navState "rubric" → FigureBlock for RubricText
│       ├── navState "{transcriptId}" → FigureBlock for TranscriptView
│       │   └── TranscriptView MainView shows full transcript
│       │       └── FigureBlock for SimulatorBio
│       │           └── SimulatorBio MainView shows full bio + tags
│       └── (default: scrolling list of transcript previews + rubric bubble)
└── RenderFigure → SubReport (methodology)
```

**Navigation experience:**
1. Start at RootReport: see project description, the bar chart as a figure, methodology section
2. Click bar chart → LinkedSVGFigure becomes main view, shows bars + selected category preview below
3. Click a bar → navState selects that category, shows CategoryAggregate as FigureBlock
4. Click CategoryAggregate → becomes main view: rubric bubble at top, scrolling transcript list below. NavState selects rubric or a transcript for preview.
5. Click transcript → TranscriptView main view: full conversation, SimulatorBio as figure below
6. Click bio → SimulatorBio main view: full persona bio, steering instructions, tags

## New Entity Types

All in a new file `src/entities.ts` within the demo package. Use a fresh namespace UUID.

### CategoryAggregate
- `label: string` — category name (e.g. "SI", "romantic_attachment")
- `dimensionName: string` — parent dimension
- `prevalence: number` — fraction of transcripts positive (0-1)
- `transcriptCount: number` — how many transcripts matched
- `rubric: fxtrId()` — weak ref to RubricText entity
- `transcripts: array(fxtrId())` — weak refs to TranscriptView entities (sorted by ID)

### RubricText
- `label: string` — dimension name
- `categoryName: string` — specific category (or "" for full dimension)
- `text: string` — the rubric description text

### TranscriptView
- `label: string` — e.g. "Transcript 0042 (claude / retirement_farewell...)"
- `transcriptId: string`
- `simKey: string`
- `familyName: string`
- `modelName: string`
- `messages: array(object({ role: string(), content: string() }))` — the conversation turns
- `simulatorBio: fxtrId()` — weak ref to SimulatorBio

### SimulatorBio
- `label: string` — simulator name
- `bio: string` — the full persona bio text
- `description: string` — how/why the sim was created
- `tags: array(string())` — structured tags
- `initialMessage: string` — opening message (or "")

## New Dispatch Handlers

### CategoryAggregate
- **MainView**: Shows dimension badge + category name as title. Rubric text in a styled bubble at top (clickable to select as navState). Below: scrolling list of transcript items (each is a compact row: transcript ID, model family, sim key — clickable to select as navState). When navState is set, shows the selected entity as a FigureBlock below.
- **FigureBlock**: Compact card showing category name, dimension, prevalence bar, transcript count. Clickable to navigate.
- **ReferenceLink**: Category name as link.

### RubricText
- **MainView**: Renders the rubric text as formatted paragraphs with the dimension/category as heading.
- **FigureBlock**: Truncated preview of rubric text in a styled box.

### TranscriptView
- **MainView**: Shows SimulatorBio as FigureBlock at top, then transcript metadata header (ID, model, sim), then renders messages as alternating user/assistant bubbles.
- **FigureBlock**: Compact card: transcript ID, model family, sim key, first user message truncated.
- **ReferenceLink**: "Transcript {id}" as link.

### SimulatorBio
- **MainView**: Full bio text (rendered as markdown-ish paragraphs), description, tags as chips, initial message in a quote block.
- **FigureBlock**: Compact card: sim name, first ~100 chars of bio, tag count.
- **ReferenceLink**: Sim name as link.

## SVG Bar Chart Construction

Server-side, build a horizontal bar chart similar to `generate_fingerprint_report.py`'s prevalence chart:
- One horizontal bar per category, grouped by dimension
- Height scales with category count (~148 categories → tall SVG, ~3000px)
- Color-coded by dimension (same palette as the Python report)
- Each bar rect has `data-preview-target={categoryName}`
- Labels on left (category name), bar length proportional to prevalence, percentage on right
- Dimension group headers as bold text separators
- The LinkedSVGFigure's `links` maps each category name → that category's CategoryAggregate entity ID

## Server Implementation

`src/server.ts` — similar structure to the demo report server:

1. Read all data at startup (results, conversations, specs, simulators)
2. Build entity graph:
   a. Create SimulatorBio entities (keyed by sim_key)
   b. Create TranscriptView entities (keyed by transcript_id, linking to SimulatorBio)
   c. Create RubricText entities (one per dimension, with full description)
   d. Compute per-category prevalence from results
   e. Create CategoryAggregate entities (linking to RubricText + matching TranscriptView IDs)
   f. Build SVG bar chart, create LinkedSVGFigure
   g. Create SubReport for methodology
   h. Create RootReport with MDX content embedding the figure + methodology
3. Pin: `"reports/fingerprint-overview"` → RootReport
4. Serve via Hono + Vite dev server on a port (3458 to not collide with demo)

## Client Implementation

`src/client/App.tsx` — near-identical to demo report client:
- `createHttpReadNamespace` + discover pins
- Create a combined dispatch handler that includes both the standard report handlers AND the new fingerprint-specific handlers
- Render `<ReportShell>`

`src/client/handlers/` — one file per entity type with the handler components.

`src/client/registry.ts` — assembles the combined dispatch handler:
```ts
createDispatchRegistry([
  // standard report entries (RootReport, SubReport, LinkedSVGFigure, etc.)
  ...standardReportEntries,
  // fingerprint-specific entries
  entry(MainView, CategoryAggregate, CategoryAggregateMainView),
  entry(FigureBlock, CategoryAggregate, CategoryAggregateFigureBlock),
  // ... etc
])
```

We can either re-export the entries from `@fxtr/reports-react-handlers` or just duplicate the `createDispatchRegistry` call with all entries listed. Since the handlers package exports a function that returns a handler (not raw entries), we'll list all entries in our registry file directly, importing individual handler components from `@fxtr/reports-react-handlers`.

Actually, looking at the code, `@fxtr/reports-react-handlers` only exports `createReportDispatchHandler()` which returns a fully built handler. We can't easily merge entries. Two options:
1. Import individual handler components from the handlers package (they're exported)
2. Use a chain of handlers (try fingerprint handler first, fall back to report handler)

Option 2 is cleaner — create a `chainHandlers(handler1, handler2)` utility that tries the first, falls back to the second. Or just build one big registry importing all the individual components.

Looking at the exports: the individual handler components (RootReportMainView, etc.) ARE exported from the handlers package. So we can import them and build a single registry.

## Implementation Steps

- [ ] 1. Create package scaffolding: `package.json`, `tsconfig.json`, `vite.config.ts`, `index.html`
- [ ] 2. Define entity types in `src/entities.ts` (CategoryAggregate, RubricText, TranscriptView, SimulatorBio)
- [ ] 3. Write server `src/server.ts`: data loading + entity construction + HTTP serving
- [ ] 4. Write client handlers in `src/client/handlers/` (CategoryAggregate, RubricText, TranscriptView, SimulatorBio)
- [ ] 5. Write client registry in `src/client/registry.ts`
- [ ] 6. Write client App in `src/client/App.tsx` and `src/client/main.tsx`
- [ ] 7. Run `pnpm install` and test
- [ ] 8. Update CODE_OVERVIEW.md
