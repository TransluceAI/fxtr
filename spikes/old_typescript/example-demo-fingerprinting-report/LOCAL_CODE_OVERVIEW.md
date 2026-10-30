# example-demo-fingerprinting-report

Interactive drill-down report for mental health fingerprint judge results. Reads judge outputs, conversation transcripts, simulator definitions, and rubric specs from a data directory, builds an fxtr entity graph, and serves it as a React app using `ReportShell`.

## Running

```bash
# Full server: load data, build entities, serve on :3458
pnpm serve <data-dir>

# Client-only (proxy mode): serve frontend on :3459, proxy API to existing server
pnpm serve:client -- --proxy http://localhost:3458

# Client-only (direct mode): frontend fetches directly from the URL (needs CORS)
pnpm serve:client http://localhost:3458
```

## Expected data directory layout

```
<data-dir>/
  results/
    {id}.json                — One per transcript. Shape:
                               { transcript_id: string,
                                 metadata: { sim_key, family_name, subject_model_name, ... },
                                 results: { [dimension_name]: {
                                   score: { [category_name]: 0.0 | 10.0 },
                                   reasoning: string } } }
  data/
    conversations/
      {id}.json              — One per transcript. Shape:
                               { settings: { sim_key, ... },
                                 transcript: [{ role: "user"|"assistant", content: string }] }
    user_simulators_uwbot.json
                             — { [sim_key]: { name, description, tags: string[],
                                 botchat_template__user_bio: string,
                                 botchat_template__initial_user_message: string|null } }
  mh_judge_lib_pkg/mh_judge_lib/data/multi_fingerprint_specs.toml
                             — TOML with [[multi_fingerprint_specs]] array entries:
                               { name, description, valid_categories: string[], is_example: bool }
```

## Entity hierarchy

```
RootReport (MDX overview)
├── LinkedSVGFigure (bar chart, one column per dimension)
│   └── each bar → CategoryAggregate
│       ├── navState "rubric" → RubricText (full dimension rubric)
│       └── navState "t{i}" → FingerprintedTranscript
│           ├── Transcript (raw messages)
│           ├── FingerprintGraderResult[] (one per dimension)
│           │   ├── Transcript (two-column: transcript left, reasoning right)
│           │   └── RubricText (collapsible)
│           └── SimulatorBio
└── SubReport (methodology)
```

## Entity types

Defined in `src/entities.ts`. All use namespace `f7a1c3e2-8b4d-4f6a-9e2c-1d5b7a3f8e0c`.

| Entity | Fields | Purpose |
|--------|--------|---------|
| `CategoryAggregate` | label, dimensionName, prevalence, transcriptCount, rubricExcerpt, rubric, transcripts | One per category: prevalence stats + links |
| `RubricText` | label, categoryName, text | Full dimension rubric description |
| `Transcript` | label, transcriptId, simKey, familyName, modelName, messages, simulatorBio | Raw conversation data |
| `FingerprintedTranscript` | label, transcriptId, simKey, familyName, modelName, transcript, simulatorBio, graderResults | Transcript + all grader results |
| `FingerprintGraderResult` | label, dimensionName, reasoning, activeCategories, allCategories, rubric, transcript | One dimension's grading of one transcript |
| `SimulatorBio` | label, bio, description, tags, initialMessage | User simulator persona |

## File structure

```
src/
  entities.ts          — Entity definitions (bicodec)
  dimColors.ts         — Shared dimension color palette
  server.ts            — Full server: data loading + entity construction + HTTP
  client-server.ts     — Client-only server (proxy or direct mode)
  client/
    main.tsx           — React entry point
    App.tsx            — Top-level app: namespace setup, dispatch, ReportShell
    registry.ts        — Combined dispatch registry (standard + fingerprint handlers)
    handlers/
      shared.tsx       — MessageBubbles component
      CategoryAggregate.tsx
      RubricText.tsx
      Transcript.tsx
      TranscriptView.tsx      — FingerprintedTranscript handlers
      FingerprintGraderResult.tsx
      SimulatorBio.tsx
```
