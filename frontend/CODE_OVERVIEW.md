# fxtr frontend — code overview

The experiment viewer: a Vite/React app in this folder that shows one **job** of the Postgres
runner as a browsable tree of workflow invocations, each a list of **step cards** with the arrays
they produced. It reads the `debug_web` backend (`fxtr/src/fxtr/runner/postgres/debug_web/`)
and nothing else; that backend also serves the built app (`dist/`), so the two share one origin
and every URL in `src/config.ts` is origin-relative. A job in progress is shown as it runs: the records on screen are polled and
updated in place (see "Live jobs").

## Running it

`README.md` covers installing the app, serving it with `fxtr view`, developing against the Vite dev
server, packaging it with fxtr, code links, and the experiment link. The code behind those:
`src/config.ts` (the origin-relative backend URLs and their `VITE_*` build-time overrides, and the
repo-root override of the editor links), `vite.config.ts` (the dev server's `/api`
proxy, which `FXTR_DEV_BACKEND` points), and on the backend `debug_web/dev_server.py` (the dev
server `fxtr view` runs from a checkout), `debug_web/viewer_build.py` (rebuilding a stale `dist/`
when a checkout serves it instead) and `fxtr/hatch_build.py` (the built app packaged into
fxtr). Nothing about a project is compiled in, so one build serves every project. `pnpm typecheck`
type-checks; `pnpm build` type-checks and builds.

## What the UI shows

`src/components/CODE_OVERVIEW.md` describes what the viewer draws (a job, its levels and cards,
card tables, the slice, the detail view, and panels) and the components drawing it.

## Layout

```
frontend/
├── index.html, vite.config.ts   the app shell (its inline script applies a kept theme before the
│                                first paint) and its build
├── package.json                 pnpm scripts: dev, build, typecheck, preview
└── src/
    ├── main.tsx, App.tsx        entry (loads the project's views, follows the viewer's events,
    │                            then renders over the live views); the job → level → card state
    │                            (the job following launches), the level's slice, the rail, the
    │                            workspace (cards overlay + the detail view's panels), and the toasts
    ├── config.ts                backend URLs (the API, the views, the events, files, EXPERIMENT.md), the repo
    │                            root override of editor links (VITE_* env)
    ├── types.ts                 the UI model: Cell, SchemaJson/ArrayTypeJson, Column, TableRow,
    │                            StepDoc, TableDescriptor/TablePage, StepArg/ExprToken, Level, LevelRef,
    │                            SourceInfo, JobSummary
    ├── api/
    │   ├── useQuery.ts          component query lifecycle: identity cancellation, coalesced revision
    │   │                        refreshes, and the recent values a remounted query starts from
    │   ├── browse.ts            cancellable component queries, key/run pages, invocation pages
    │   │                        (each invocation with what it produced, sorted and filtered on
    │   │                        the server), batched entity metadata, and the invocation behind
    │   │                        each of a page's rows (aliased run pages)
    │   ├── graphql.ts           fetch-based GraphQL client (identical requests in flight share one
    │   │                        fetch), the documents, response types,
    │   │                        `fetchVersions` (the aliased `invocationVersion` poll)
    │   ├── cbor.ts              DAG-CBOR decoding; CID / bytes / struct / list predicates
    │   ├── live.ts              the live store: payload-free overviews and levels, separate complete
    │   │                        invocation/step entries, kept current while watched; the poller;
    │   │                        `retryAndReset`, what every retry button calls
    │   ├── finishes.ts          what finished when, as this page has seen it: each run's finishes of
    │   │                        the last few minutes, from successive overview fetches
    │   ├── events.ts            the viewer's events: the /api/events WebSocket, reopened with
    │   │                        backoff when it closes, and onViewerEvent (views and job events)
    │   └── resources.ts         the promise cache for source info, code files, and the project's EXPERIMENT.md. A failed load stays cached, so it is requested
    │                            once and shown as an error, until a retry forgets it
    ├── model/
    │   ├── level.ts             buildLevel: overview metadata → StepDoc[] with output query references; argument
    │                            expressions over derived nodes; each
    │                            card's progress, the level's glyph, liveness, and awaited node
    │   └── schema.ts            formatSchema / formatArrayType / formatType (in full), typeLabel
    │                            (at a glance: `struct`, `Report`, `Array[struct]`), returnType (the
    │                            output type without the run's mapped and replica dims), columnsFor,
    │                            column keys, schemaAt (the schema at a path in a value) and
    │                            isReference
    ├── lib/                     small pure helpers: invocations (dim kinds, invocation keys, a
    │                            step's part of the level's slice, on its invocation dims or
    │                            of every dim of its rows, view-system spawns),
    │                            output (a node's output as its panel reads it: its output source
    │                            for a slice, whether its values are entities, the values as
    │                            view-system data, and the queries for the selected invocations'
    │                            count, a bounded sample of rows, and a table's page, each page
    │                            decoded once however many read it),
    │                            levelState (the level's slice and each card's table state as a
    │                            store, as a context), store (Store, a
    │                            value outside React state read by selector, and
    │                            useStableCallback), launches (useFollowLaunches: a page switches
    │                            to each launched job unless someone picked one in it, and says
    │                            why in a toast), debounce (useDebouncedInput, typing applied
    │                            once it pauses), dismiss (useDismiss: a press outside or Escape
    │                            closes), cardTable (a card's table state
    │                            for the slice and its page, read by the card,
    │                            the page of its invocations with what each produced, and
    │                            the invocation behind each row of a page),
    │                            scrollAnchor (holding a card still on screen across a slice
    │                            change), columnWidths (the widths a card table's columns start
    │                            at), keyLists (the level's dims and where the
    │                            slice panel reads each one's values, a node's own dims, and the
    │                            loaders of a dim's value pages), panelView (a node panel's view options and
    │                            its default view),
    │                            linkBase (link places, and the links a card's rows and
    │                            invocations open: a node's location at keys, `invocation`),
    │                            steps (index, pathname labels, a pathname's mapped key), progress (run states, glyphs, counts,
    │                            a workflow's progress, and a run's finished share, rate and time
    │                            left), ring (a progress arc's path), tabIcon
    │                            (the tab's icon as the workflow's progress ring),
    │                            kindIcons (the node kind → icon map, and the entity's and invocation's icons), clock (the shared 1s tick),
    │                            fresh (ids new since the last render, for fade-ins), values
    │                            (shape-driven summaries and one-line container previews), code
    │                            (editor links), editor (the picked editor, kept in localStorage),
    │                            time, cx, sets, keyboard (editable targets, the platform
    │                            modifier label and exclusive Command on Mac / Control elsewhere
    │                            matching), tableKeys (j/k in a grid: the step's key, the lit
    │                            chip, and clicking the next one in its column), modifierClick
    │                            (the root's attributes while the platform modifier or Alt is
    │                            held), markdown, theme (the colour theme and its toggle's storage);
    │                            loading (staged views wait for queries and nested renderer fallbacks);
    │                            cardSpotlight (the context a chip lights up its
    │                            minimap row, and its card in place of the chip's hover preview, through, over
    │                            a store each row and card reads its own part of);
    │                            openEntity (the context a link is followed
    │                            through, from a panel or a root); openLevel (the context
    │                            a view shows a workflow invocation's level in the trace
    │                            through); networkSimplex (the exact
    │                            solver behind the flowchart's horizontal positions)
    ├── panes/                   the detail view (see "The detail view: panels and addresses")
    │   ├── address.ts           panel addresses (a view, a location, params): the syntax (parse,
    │   │                        format), relative locations (`Rel`, applyRelLocation), the parts a
    │   │                        head draws
    │   ├── resolve.ts           an address against the whole job and the registry (`Catalog`): the
    │   │                        params it uses, the target a panel renders (`Target`), or why there
    │   │                        is none
    │   ├── catalog.ts           useJobCatalog: every level of the job from the live store, loaded
    │   │                        and watched as addresses ask for them
    │   ├── columns.ts           the layout (columns of panels) and the panels' sequence, as pure
    │   │                        rewrites: insert, move, close, pin, navigate, history
    │   ├── useColumns.ts        the columns as state: following links, what every panel resolves
    │   │                        to (`resolveColumns`), link states, highlights, reveals
    │   ├── placement.ts         where a new panel goes (`autoPlacement`)
    │   ├── ColumnsView.tsx      the columns on screen: sliding, resizing, dropping, animation
    │   ├── panelSizing.ts       how tall the panels in a column are
    │   └── drag.ts              the drag payload: a link (`LinkTarget`) or a panel being moved
    ├── views/                   the shell side of the view system (see "The view system")
    │   ├── FrontendViewShellProvider.tsx  the ViewShell this app provides (useEntity,
    │   │                        useInvocation, useLink, EntityLinkImpl, useEntityLinks) and the
    │   │                        RegistryProvider
    │   ├── registry.ts          loadViews: the DispatchRegistry, the defaults plus the renderers of
    │   │                        the served project's view bundle (a given build of it), and why
    │   │                        they are missing; its stylesheets; isProjectRenderer
    │   ├── liveViews.ts         the renderers in use as `fxtr view` rebuilds them: the store
    │   │                        (useLiveViews), ViewsFollower (loads each announced build and
    │   │                        swaps it in), startLiveViews (over the viewer's events)
    │   ├── entity_loader.ts     the one loader of entity bytes (POST /api/entities, batched,
    │   │                        decoded as DRISL)
    │   ├── invocation_source.ts useInvocationRecord, the shell's useInvocation: an
    │   │                        InvocationRecord from the live invocation entry (a workflow) or
    │   │                        the live step pin plus its StepRequest entity
    │   ├── EntityLinkImpl.tsx   the shell's EntityLinkImpl, the inline link view as a chip: click
    │   │                        follows the link through the nearest OpenEntityContext, hover
    │   │                        shows its EntityHoverPreview + cid
    │   ├── viewLink.ts          the shell's useLink: a renderer's link to a declared view, a
    │   │                        location relative to its view, or both (`specTarget`, which
    │   │                        `EntityLinkImpl` shares)
    │   ├── EntityPanel.tsx      an opened entity: RenderEntity in the EntityPanel slot, the
    │   │                        renderer dropdown (in a panel, its address's view)
    │   ├── DefaultInvocationPanel.tsx  an invocation opened in a panel with no renderer chosen:
    │   │                        its status line and provenance, its inputs as rows that
    │   │                        expand to their values, a workflow's runs, then its output
    │   │                        inline (a sketch while it runs, its error when it failed);
    │   │                        a workflow's has Open in trace, showing its level
    │   ├── RendererInfo.tsx     the info icon ending a renderer's row in a view dropdown: a
    │   │                        card naming the registry component (the "renderer") behind it
    │   └── EntityLabel.tsx      an entity's EntityInlineLink view without interaction (breadcrumbs)
    ├── components/              the UI's components (see src/components/CODE_OVERVIEW.md)
    ├── debug/                   dev-server-only tools (see "Run replay"): replay.ts (a recorded
    │                            level's invented timeline, each invocation's state on it, and
    │                            the level's overview at a point of it), replayData.ts (the
    │                            replay's state and the request middleware answering from it),
    │                            RunReplay (the rail section and its clock)
    └── styles/                  tokens.css (the palette, light and dark), primitives.css (shared
                                 classes), and
                                 views.css (the view system's own: fxtr-view's classes and the
                                 opened-entity frame; renderer packages style themselves)
```

## How the pieces connect

- `App` loads the job named in the URL hash directly (newest otherwise), pins the chosen job id
  for later refreshes, and follows launches (`lib/launches.tsx`): each job a launcher announces on
  the viewer's events (`api/events.ts`) replaces the one shown, unless someone picked a job in
  the page (in the job picker, or by changing the hash), and a toast (`components/Toasts.tsx`,
  over sonner) says why and offers the way back, or the new job. It renders `JobView`, which holds
  the stack of opened levels (`LevelRef[]`) and asks `useLevel` for the current one; it also holds which view of the level is on screen (`pageView`: the trace, or an
  `InvocationPanel` renderer chosen in `LevelViews`) and whether the trace is replaced by the
  flowchart/selected-card vertical split. Flowchart mode hides the rail and moves its job, experiment link,
  breadcrumb, mode, and custom-view controls into a floating card over the graph. Level changes run in a transition so the current level stays up while the next
  loads; the first load of a job shows the picker beside a loading message. `JobView` also holds
  the detail view's columns (`useColumns`), and the level's shared state (the slice and each
  card's table state, provided as `LevelStateContext` around the cards
  and the panels). Cards are keyed by pathname and node identity, since identities are
  content-addressed and can recur across levels. `StepCard` is memoized, with
  callbacks of fixed identity, and what changes often lives in stores read by the few components
  it concerns (the viewport, the spotlight, card table state), so a hover, a scroll, or paging one
  card re-renders only what it changes.
- **The workspace** is the card column and the detail view as a resizable `PanelGroup`, whose
  divider is always draggable, split evenly to begin with. It is never laid out narrower than
  `WORKSPACE_MIN_WIDTH` (the trace's 600px and room for a detail view beside it); a window leaving
  it less scrolls it sideways, beside the rail, rather than the page. In the trace, the scroller holding the level is not inside the left
  panel but laid over the workspace from its left edge (the left panel only measures the width the
  cards are laid out in), as wide as the left panel; the resize handle sits above it. The trace is
  never laid out narrower than `MIN_TRACE_CONTENT_WIDTH` (600px), but while a panel is open the left
  panel can be dragged down to `TRACE_COLLAPSED_WIDTH` (100px): the trace keeps its width and lies under the detail
  view, which is raised over it with a shadow, while the trace is dimmed and inert (a click
  anywhere on it widens the panel back to `TRACE_EXPANDED_WIDTH`, a pixel over 600px to absorb rounding, and reaches
  nothing inside). With no panel open, the left panel's minimum is `TRACE_EXPANDED_WIDTH`, so the
  empty detail view never covers the trace, and closing the last panel widens a collapsed trace. These minimums are
  pixels converted to panel percentages, recomputed as the workspace's width changes. The widths
  are CSS variables `JobView` sets on the workspace from a `ResizeObserver`, so dragging the
  divider does not re-render the level.
- Links open through the nearest `OpenEntityContext`: the trace's and the rail's from their roots,
  a panel's from that panel (see "The detail view: panels and addresses").
- `useLevel` (`api/live.ts`) reads a level built from its overview entry. `buildLevel` publishes
  the graph and counts immediately, with references for later key/output queries. Changed
  overviews rebuild the level without loading arrays.
- Overview and complete-record reads suspend on their first load under the nearest `Suspense` (immutable data
  through `resource(key, load)` in `resources.ts`, records through the live store's entries), so
  components stay synchronous; a failed first load surfaces through the nearest `ErrorBoundary`
  with the backend URL it was talking to. Paginated queries use local loading/error/retry states
  and abort on unmount. Later overview updates never suspend (see "Demand loading and live jobs").
- Docs and code load per card, and for the workflow in the level's header (`SourceDocs`), from the backend's `/api/source/{name}`, asked
  with the level's `codeVersion` (on the `Level` and every `StepDoc`): where the code the job ran implements the
  function, at its pinned commit, falling back to the backend's working tree for a function the
  code version does not place in the repository. The "implemented" chip is a link (`codeTarget`
  in `lib/linkBase.ts`) to the `code` view of the node's step path, or of `/` for the level's own
  workflow: a panel (`CodeBody` in `CodePane.tsx`) showing the file from `/api/file` at that
  commit, its lines marked, whose head has a split **Open** button choosing the editor
  (`lib/editor.ts`). It only knows where a function is implemented;
  where a node was scheduled (the call site) is not recorded anywhere yet.

## Run state

Every step, invocation, and job is drawn with one **state glyph** (`components/StateGlyph.tsx`),
whose shape is its run state (`lib/progress.ts`): a dashed ring for `waiting` (a run whose inputs
have no values yet, so it has no spawns), a ring for `queued`, a ring in ink for
`running` (a spinner on summary glyphs whose ring shows no progress), a solid dot for `done`, a × for `failed`, a ring with a bar for `stopped` (interrupted, held, stopped). Colour
means only failure. A running or queued run's ring is also its progress: a light track with an arc,
clockwise from the top, for the share of its invocations that have finished. Done is the normal
case and says nothing: only the minimap draws done dots (and draws a running step as a solid dot
in ink rather than a ring); everywhere else a done glyph
is omitted (`StateGlyph` keeps its room with `reserve` where labels must stay aligned). Anything standing for several invocations (a card, a key value, a level, a job)
takes two channels: its **shape** is the most active state present (`running > queued > waiting >
failed > stopped > done`) and it is drawn in the danger colour when anything in it failed, so
neither activity nor a failure is ever hidden. Steps are told apart by name, not colour.

- **Where it shows.** The job picker's list (not its button), the minimap rows, the argument chips' hover previews
  (shown when the chip's card is not fully on screen: the glyph, a run's ring, and its counts; the
  chip itself carries the kind icon), and the level header. A run's card head and its flowchart
  node show its progress line instead (below). The
  job picker shows a check for a job that succeeded. The tab's icon is the workflow on screen as a
  progress ring (`lib/tabIcon.ts`): the mean of how far each of its runs has got
  (`workflowProgress`), in its glyph's colour; the tab's title is the workflow's name alone. The minimap's dependency focus is its edges;
  while a row is hovered, every glyph outside its dependencies fades (its colour kept). A minimap
  row's name and dim pips are faint until its step has started (any invocation past queued); the
  name is red once any invocation has failed.
- **A run's progress line** (`components/ProgressLine.tsx`): a 3px line along the card head's
  bottom edge and the foot of its flowchart node, while anything in the run is left to finish (a
  card's, for runs of more than one invocation). Finished work is dark grey, failed red, in flight
  light grey, and the rest the track, like a video player's played, buffered and remaining
  (`--progress-*` tokens). Each time a level update shows more of the run finished, a sheen sweeps
  the finished part once, so a run that is moving shows it even when its percentage does not. On the
  card head, `RunStatus` says the rest in words: the share finished (`43%`, `done` once nothing is
  left), and only what is wrong: `14 failed` (`261 of 381 failed` once over a quarter fail),
  `3 stopped`; a run of one says its state (`running · 2m14s`). The percentage opens a hover card
  (`RunProgressCard`): a heartbeat of the last minute's finishes, the legend of the line's colours
  with each state's count beside the rate, time left, last finish and longest running, then a row
  per error type. Flowchart nodes have the line but no words.
- **The running hatch** (`.hatch` in `styles/primitives.css`): light diagonal stripes sliding
  sideways, behind a running card's whole head (whose muted text, the kind, status and buttons, is
  drawn in ink over it), a running step's minimap row (unless a chip lights it), and the
  running-step indicator. The indicator (`components/RunningIndicator.tsx`) floats over
  the trace while steps are running but none of their cards is on screen: a pill naming the
  running step nearest the viewport, at the top middle with an up arrow when it is above, at the
  bottom middle with a down arrow when below, its progress line along the pill's foot. Clicking it
  jumps to the card as a chip does.
- **Motion.** Running summary glyphs (not key cells, chips, or the minimap) whose ring shows no
  progress are spinners, turning at the pace of the table's loading spinners. Timers tick on one shared clock (`lib/clock.ts`). Cards,
  and table rows new since their view last rendered fade in once (`lib/fresh.ts`). A card lit up
  by a hovered chip pulses its glow, and the minimap row a chip names its fill, on one shared pace
  (`--spotlight-period`). A progress line's sheen sweeps once per update that brings finishes, and
  the hover card's heartbeat slides left a whole pixel at a time (by fractions of a pixel it
  shimmers). The running hatch slides one tile sideways a second. `prefers-reduced-motion` turns these animations off (a lit card keeps a still glow, the
  heartbeat moves only when it redraws).
- **Kind icons** (`lib/kindIcons.ts`, `KindLabel`): step `SquareFunction`, workflow `Workflow`,
  source `Import`; monochrome and beside the kind word (plain text, no pill)
  on the card head and level header (at 14px), the default invocation panel, and flowchart nodes,
  and beside the step's name in argument chips. The return card's `returned` label is plain text
  too. The flowchart toggle uses `Waypoints` so it is not mistaken for a workflow. A view of the
  project's own is marked `Component` (`CUSTOM_VIEW_ICON`), on the card's panel button and over
  the project's part of a view dropdown's menu, which is where its meaning is learned.

## Demand loading and live jobs

The trace reads `levelOverview`: graph nodes and types, compact attempt status, returned/awaited
node identities, aggregate invocation counts, and for each run when the job's attempts at it first
started and last finished, how many it finished, when the longest-running one started, and its
failures grouped by error type; `asOf` is the database's time of the read, which `buildLevel` uses
to put those times on the browser's clock (`RunActivity`). `api/live.ts` keeps these overview entries
separate from the complete invocation entries used by custom renderers. Building a level never
requests array rows. `StepDoc.output` and `StepDoc.browse` identify deferred queries; cards contain
no output rows or complete spawn lists. `TableDescriptor` labels a table and `TablePage` adds its
displayed rows. Overview nodes carry mapping/replica dimension names and status totals, without
replica value lists. The GraphQL schema defines summary, page, source, filter, and sort contracts
explicitly; complete `GqlInvocation` records belong to the custom-renderer boundary.

A card's table reads its rows ten at a time (`useOutputPage` over the node's output source in the
slice), and its key columns' headers request searchable lists of the
first 1000 matching dimension values (`KeyValueList`, reading `outputDimValues` for a dim the node's invocations do
not key; the slice panel's lists behave the same). The headers' value
counts come from a slice-independent page, so a pick elsewhere in the level does not
refetch them; the value columns query only while shown, stay mounted (hidden) once opened, and
refresh compatibility against the other picks when they are shown again. Other picks update
compatibility and status counts without removing values: incompatible values stay dimmed, and each
column retains its search, page, scroll, and rows while counts load. A card counts the invocations
the slice picks out with a one-item `runPage` (whose total is the count, and whose item is the
invocation a count of one names); the header checks a picked value exists on the
node only once that count is zero. `test/browse.test.tsx` covers the headers' reads.

A node's panel reads the same way: its tables are `PagedTable`s over the node's output source
in the level's slice, its "not finished" line is a `runPage` of unfinished invocations, and
an `InvocationPanel` renderer is handed every spawn in the slice (`fetchAllSpawns`, pages of 200).
Whether the slice picks out one row or several is read from its first two rows
(`useOutputRows`). Slice-driven reads keep the previous slice's
answer until the new one arrives, so panels do not flash a loading state on a pick;
a table keeps its rows, filters, and sort, and reads the new slice from its first page. The same
read made by a node's card, panel head, and panel body at once (their selected run, on a
pick or a refresh) is one request: `gql` shares an identical request while it is in flight, and
aborts it only once every caller has.
Output counts may be unknown until arrays are inspected; invocation counts always describe the
complete run.

`components/PagedTable.tsx` supplies 25-row pages to `TablePanel`, retaining its cells, column
sizing, visibility, and expand controls, and leaves out the key columns its surroundings name
(`hiddenDims`). Key lists size their viewport from
the values read, at most ten rows. `Pagination` supplies an editable 1-based starting row, previous/next and
first/last arrows, and a loading spinner.
Entered row numbers clamp to valid bounds and open the 25-row page containing that row. Output queries return their actual offset, clamping
overshoots to the last page; an explicit last-page request can scan to discover an unknown total.
Page, filter, and sort requests retain the previous rows and their range with a loading spinner
until the new results arrive. Server pages swap without row arrival animations. `useQuery`
scopes retention to the output source less its slice, so unrelated outputs cannot inherit rows.
Filters and sorting cover the complete sliced output on the server. Each run's ledger-based
revision detects target changes even when counts stay equal, while unrelated graph updates leave
its pages alone. Run changes refresh
the current page, retaining controls and previous values while loading. `api/useQuery.ts` coalesces
version changes and manual refreshes into one follow-up after an active read finishes. Initial
loads and query changes block navigation; background refreshes show a spinner while the displayed
page remains interactive, and slow reads can complete. Query identity changes and unmounts cancel their active reads.
`useQuery` also keeps the last value of its 500 most recent keys (`recent`), so a query mounted, or
changed, to a key read before starts from that value and revalidates it as a refresh: a view that
remounts (a panel reopened, a level returned to) shows its data at once instead
of loading. A key must therefore name what it reads wherever it is used, so every key is tagged
by its read (`['output-page', …]`); tests clear the kept values after each test (`test/setup.ts`). Pages use current results,
so additions can move rows between pages. Navigating away aborts the request. Generic
array entity panels use this paged path; explicitly selected array renderers can still load
complete arrays.

Entity chips use batched type-tag metadata instead of payload dispatch. Hovering or opening an
entity invokes its registered payload renderer. Custom invocation renderers receive complete
spawn collections only when selected; their `useInvocation` and `useEntity` contracts are unchanged.

The job picker searches and pages server-side. A job named in the URL is looked up directly,
independent of the picker's page. With no URL selection, the first loaded job is pinned before
periodic refresh. `Jobs` refreshes its summary every 30 seconds and, when a launch event names
the selected job, refreshes both the summary and live records (`pokeLive`). This is independent
of the navigation and toast policy in `lib/launches.tsx`, so a resume refreshes a settled job
even when the tab stays on it and the job finishes before another summary poll. While watched
data is live, the live store refreshes overview metadata every 1.5 seconds, pauses in hidden tabs, and
backs off after errors. A forced refresh waits until the tab is visible before it is consumed.
Every overview fetch, changed or not, is recorded in `api/finishes.ts`:
the counts carry no finish times, so each fetch's new finishes are placed evenly across the time
since the fetch before, which is what the heartbeat and the hover card's rate read. That record
starts when the page first fetches the level. Changed overviews and forced refreshes invalidate watched step pins,
including stopped/failed/held pins after a resume. Complete invocation entries use version polling. Refreshes preserve
the slice, the panels, and scroll, and refresh only each mounted table’s current page.

## The detail view: panels and addresses

The detail view is columns of panels (`src/panes/`). What a panel shows is its **address**, plain
data with one canonical spelling. The panels form one **sequence**: a panel either **browses**,
following the panel before it, or is **pinned**, and changes only when the user changes it.

- **Addresses** (`panes/address.ts`, whose module comment is the syntax's reference) are a
  **view** applied to a **location** and **params**, written like a call:
  `pander-score.Elicitations/batch(/generate_prompts, elicitor=sonnet)`,
  `(/prompts.output.call, prompt=3)`, `tree(cid:<cid>.messages[2])`,
  `limerick-contest.poet(poet=nash)`, `code(/judge)`, `experiment_readme()`. A location is a step path from the job's root workflow
  (`/` alone is the root), or an entity's cid, then a **field path**: a step's starts in each
  invocation's record (`.output…`, `.inputs.<name>…`). The view is a renderer's id (a view declared
  within another is named below it), a page's, a built-in (`VIEWS`: `table`, `invocation`,
  `tree`, `trace`, `flowchart`, `code`, `experiment_readme`), or nothing for the default. Params are flat, by name. A
  **relative address** (`Rel`) names a target from another: a view, and a location that is the
  same, a field path below (`.call`), a sibling step (`../step`), or absolute.
- **Resolution** (`panes/resolve.ts`) works an address out against the whole job and the registry
  (`Catalog`; `panes/catalog.ts` builds it from the live store, loading and watching each level an
  address asks for). A step path is walked from the root level, each workflow step on the way
  entered through the run its mapped keys pick out (`childPathname`, the runner's naming): a key
  left unbound, or a level still loading, is a problem of the params, which the panel shows and
  waits out. It picks the bindings the address uses (the keys walked, its node's dims, its view's
  declared params), writes in a node's default view when the registry decides it, picks the
  `Target` the panel renders (a node through a view, with the source node of each argument that is
  one node's output; a value at a field path; the root workflow's own invocation; an entity; a
  page; the experiment; the code of a node's function, from its `method` and `codeVersion`, or of
  the level's workflow for `/`), or says why there is none, and whether that is the address's fault
  (`structural`).
- **The sequence** (`panes/columns.ts`): every panel has a place in one sequence (`prevId`), after
  the roots: `@trace`, at the centre's address (`Centre`: the level's workflow through the trace,
  the flowchart, or a view of it), and `@rail`, at none; both bind the slice. A browsing panel
  stores its address relative to the panel before it (`rel`) and the params it binds itself
  (`own`); its bindings are that panel's with its own over them (`resolveColumns`), so it follows
  that panel, and through the panels before it the slice. A pinned panel stores its address whole
  and every binding it has; the browsing panels after it (its **followers**, up to the next pinned
  panel) follow it. A browsing panel showing a view declared within another names nothing unless
  the panel before it shows that one, so it closes when that one switches away. The pin button
  pins a panel where it is, for good (`columns.ts`'s `pinPanel` says why there is no unpinning); a
  pick inside a browsing panel that leaves out a binding it inherits pins it too (`setBindings`).
  Closing a panel closes its followers. Each panel keeps a history of what it stored, for back and
  forward; pinning drops it. **Navigating** a panel (a click in the view before it, its view
  switcher, its params, back and forward) keeps its followers, which follow it, only while it stays
  at the same location (`sameKind`: its view and params may change); moving to another location
  closes them even where they would still resolve, and keeps them with the history entry it left
  (`Visit`), so going back there opens them again where they were. Moving a panel on screen never changes the sequence; one dropped in
  place of the panel it follows comes after the panel before that one instead.
- **Following a link** (`useColumns`'s module comment has the rules): a plain click reveals the
  browsing panel right after the view, or a pinned one, when it already shows the target; else
  navigates the browsing panel right after the view there, else opens one right after the view
  (before a pinned panel there). Alt opens a pinned panel beside the view, in the sequence after
  the view's followers. A drop opens a panel where it lands: browsing, right after the view, when
  the view has no browsing panel after it yet; else pinned, as Alt does. A link from a root is
  stored absolute, binding the level's keys itself, so moving between levels changes what no panel
  shows. After a change, the first browsing panel whose address names nothing closes, with its
  followers. A path through an entity reference is checked against the data once it loads:
  `PathValue` reports a field the value lacks through `PathMissingContext`, and a browsing panel
  closes the same way (`namesNothing`). A link carries its row's full keys: the panel keeps only
  what they add to its view's bindings.
- **Where links come from.** The trace's links (card rows, open buttons, the minimap, a view of the
  workflow in the centre) open from `@trace`, the rail's (the experiment) from `@rail`
  (`lib/linkBase.ts`). A panel's links open from it: its rows as itself at the row's keys, its
  entity chips as the value at their place relative to it (`.output…` in a node, `.…` below a
  value), and a renderer's `useLink` / `EntityLink` at the view and location the renderer names
  (`views/viewLink.ts`, which refuses a link to a view declared within another from anywhere but
  that one). Every link that opens a panel on a click can be dragged into the
  columns too, a table's rows among them (`dragLink` in `lib/openEntity.ts`).
- **What shows the navigation.** A panel's head (`PanelHead`) is one line that is its resolved
  address in words, part by part (`addressParts`: the steps, the field path, a nested view's own
  name), with the parts it shares with the address of the panel before it dimmed
  (`sharedParts`); then its own params as chips (on a line of their own when they do not fit
  beside the path), the params from the slice only counted (`slice n`), and those it inherits from
  the panel before it left out; then its pin (filled on a pinned panel; on a browsing one, a button
  shown while the pointer is over it), back and forward, its view switcher on a step's panel (which
  changes its address's view among its output rows and the entity renderers of its output's type;
  shown where there is a choice, and not on a panel showing one invocation, a renderer of the
  step's function, or a view of the project's own; never on a panel of a field path), and close. A
  blue top edge marks a browsing panel. Hovering the path or the count opens the address card: what
  its icon means, what it stores, every binding by source (dim when its address only passes it
  on), dropping its own, and the canonical address. A link is lit in selection blue while the
  browsing panel right after its view shows what it names, and dashed while a pinned panel does.
  Hovering a link outlines the panel it would navigate or reveal, or the line where it would open.
  A panel's border flashes blue whenever what it shows changes, whatever changed it
  (`ColumnsView`).
- **One place for each view choice.** A panel's view (an entity panel's renderer, the view a
  node's panel shows its output through) is its address's view, so back and forward return to it.
- **Layout** (`ColumnsView`, `placement.ts`, `panelSizing.ts`): columns slide over the trace,
  resize, and take drops; each panel renders once into a node of its own that its slot holds, so
  moving it keeps its state. A slide (wheel deltas added up to one a frame) renders only the
  columns' window again: the strip of slots and the panels are memoized, so no panel renders and
  nothing is measured while the columns slide, and `--slide` is set on the slice control alone
  (`App`), the one thing that reads it.

The layout is per-session state, not persisted. It is kept when another level is shown, and so is
the slice.

## The view system

Entities are rendered by the view system in `ts/` (`fxtr-view` and the packages around it, see
`ts/CODE_OVERVIEW.md`); this app is a *shell* of that system. Renderer
code talks to its host only through `fxtr-view`'s `ViewShell` context, and `src/views/` is this
app's implementation of it. Beside it the app mounts `fxtr-view-shell-util`'s `RegistryProvider`,
which serves the dispatch components (`RenderEntity`, `RenderInvocation`, `useRenderers`) the app
renders slots with. The app is compiled with the viewer's own renderers only
(`fxtr-view-default-renderers`); the renderers of the project the backend serves come from that
project's view bundle, loaded at runtime by `views/registry.ts`'s `loadViews`. It fetches the
manifest from `VIEWS_BASE`, checks the contract version before running any of the bundle's code,
links the stylesheet, imports the module, calls its factory with this app's instances of the
shared modules, and concatenates the renderers after the defaults. A 404 means the project has no
renderers of its own; any other failure (no build yet, another version, a module that throws,
duplicate ids) leaves the app on the defaults, and `ViewsNotice` in the rail says why. If a
renderer build fails, including at server startup, the backend serves its last valid bundle
while continuing to watch for source fixes. `ViewsNotice` reports build errors from the viewer's
events. A renderer not among the defaults is the project's own (`isProjectRenderer`).

**Rebuilt renderers replace the old ones in place.** `main.tsx` awaits the first load before the
first render, so the first paint never shows the defaults in the project's renderers' place. Then
`views/liveViews.ts` follows the viewer's events (`api/events.ts`, the `/api/events` WebSocket), on
which `fxtr view` announces each state of the bundle as it rebuilds it
(`debug_web/CODE_OVERVIEW.md`): its version, and whether it is building or failed (with the
build's error). For a version other than the one loaded,
`ViewsFollower` loads that build beside the current one (the manifest's `X-Fxtr-Views-Version`
names an immutable snapshot, and the files' URLs select it). `loadViews` prepares the module and
stylesheet together, registering stylesheet listeners before inserting its inactive link and
bounding asset loads with a timeout. Only a complete load is activated and replaces the registry;
failed candidates are removed while the current renderers and styles stay. An expired manifest
(410) restarts at the current build; asset failures retry only when the current manifest names a
different, not-yet-attempted version. Each announcement is handled once even when recovery loads
a newer version, preventing repeated loads while events catch up. Loads run one at a time, with
the latest announcement received during a load handled afterward. `main.tsx`'s `LiveApp` reads
the store, so the new registry reaches `FrontendViewShellProvider` as a prop, through `App`,
`Jobs`, and `JobView`; everything else (the job, level, panels, slice, the entity cache, the
storage atoms handed to renderers) stays, while dispatch remounts what it rendered through the old
registry (`fxtr-view-shell-util` keys it by registry), so renderer components start over and a
renderer that threw gets another chance. A level's custom view and an invocation panel read their
renderer back against the registry (`chosenRenderer`), falling back when a rebuild removed it.
`ViewsNotice` also shows a build running, a build's failure (its output scrolled to its end, the
last build still shown), and briefly that the renderers were updated.

- **View dropdowns** (`ViewSelect`, under a node panel's head (`StepControls`) and `RendererSelect`) are a radix
  `Select` with a two-line trigger: a small "views" label, with a neutral badge counting the
  project's views behind the custom-view icon when there are any, over the name of the view shown. The menu lists the viewer's
  own views (host views and default renderers) first, then the project's under a "from this
  project" heading carrying the custom-view icon. A node's panel counts its output and invocation
  renderers together.

- **`FrontendViewShellProvider`** builds the `ViewShell`, mounts it with the `RegistryProvider`, and
  is mounted by `JobView` around the whole body (the rail asks the registry for the level's views,
  so it needs the registry too): the registry is `loadViews`'s, as a prop; the `useEntity` hook is
  `createCachedEntityHook(loadEntity)` over `views/entity_loader.ts` (the batched DRISL loader
  from `fxtr-view-shell-util` over `POST /api/entities`, so a panel loading thousands of spawn
  outputs sends a few requests at a time rather than one per entity); the `useInvocation` hook is
  `useInvocationRecord` from `views/invocation_source.ts`, which reads the live store, so a
  renderer showing an invocation in progress re-renders as it moves; the entity cache is created
  once at module level so it lives for the page, and so is the shell value itself (nothing in
  it depends on the app's state; navigation is `EntityLinkImpl`'s). Ids in this shell are content-only, so a `cid` string is an
  entity's whole identity, and an address names an entity by it alone (`cid:…`).
- **`EntityLinkImpl`** is the shell's `EntityLinkImpl`: every link (in cells, trees, argument
  headings, and the renderers' own references) is a type-labelled metadata chip: click follows the link through the
  *nearest* `OpenEntityContext` (from the panel it is in, or from a root such as the trace; Alt pins),
  the chip is lit in selection blue while the browsing panel after its view shows what it names (and
  faintly while a pinned panel does), and hovering opens a radix
  `HoverCard` whose body is the entity's `EntityHoverPreview` view followed by its full cid. A chip is
  also a drag source for the detail view's columns (`panes/drag.ts`), and starting a drag dismisses
  the hover card.
  The shell's `useEntityLinks`, for a renderer's own marks, follows the entity alone
  (`linkTarget(cid, null)`) through the same nearest `OpenEntityContext` and reads
  `linkState(...).open`, so a mark rings as its chip is lit.
- **`EntityPanel`** first reads type metadata. An `Array` uses the host's paged table by default;
  selecting a registered renderer invokes its complete-data view. Other entities dispatch through
  `RenderEntity` in the `EntityPanel` slot with a per-mount storage atom: a type's own panel when
  registered, the data tree otherwise. (A node's panel dispatches its one output entity to the same
  slot, choosing the renderer from its own dropdown.) Its
  corner carries the renderer controls. A `RendererSelect` switches between the type's views
  (`useRenderers`: its own panels, then the data tree, the wildcard), shown even when the tree is
  the only one so the choice is always visible; the choice lives with the mount and is not
  persisted.
  Every view dropdown (`ViewSelect`) ends each renderer's row in **`RendererInfo`**, an info icon
  showing on hover the component behind that view (its *renderer*), the type it is registered for,
  or that it takes any type. The dropdowns hand each row the renderer it chooses; dispatch never
  wraps its renderers in shell chrome, so the whole affordance is shell-side and the library stays
  unaware of it. **`EntityLabel`** uses batched type metadata for links and breadcrumbs, without loading bodies.

## Invocation renderers

Besides entity types, the registry holds views of step and workflow *invocations*, keyed by the
function's registered name (`fxtr-view`'s invocation slots; see `ts/CODE_OVERVIEW.md`). This
app gives them two surfaces, both reading the registry through `useRenderers`:

- **In a node's panel** (`StepPanel`, `DetailPane`): a panel whose address names an
  `InvocationPanel` renderer of the run's function (`StepDoc.method`, the registered name, never
  the local node name), chosen from the step card's views button, renders
  `<RenderInvocation slot={InvocationPanel}>` with every spawn the level's slice picks out of
  the step (loaded in full with `fetchAllSpawns` once the view is chosen: all of them for no
  slice, one for a slice on every invocation dim), remounting when the spawns change so a caught
  error does not outlive the slice, with a storage atom per node and view.
- **Detail views** (`fxtr-view`'s `declareView`): views reading more params, linked to by their
  declarations (`useLink(VIEW, params)`), each opened beside the panel of the view linking to it. A
  detail declared within a view is named below it and linked to only from it. Where the shell
  cannot open views (a test shell), the link is null and the renderer shows its detail in place.
- **As the level** (`LevelViews`, `App`): the workflow's `InvocationPanel` renderers, listed
  under "custom views" right below the minimap's rows (not a view declared within another or one
  that needs params bound, which only links open); a renderer replaces the header and cards with
  `<RenderInvocation slot={InvocationPanel}>` given the level's own invocation as its one spawn
  (`Level.resultCid` as its output), inside the card column's
  `OpenEntityContext` so its entity chips still open the detail view. The minimap keeps drawing
  the cards (with no geometry to map) but no selection or focus. Reset on every level change. The
  centre's address then names the renderer, so its relative links resolve against the workflow's
  location. A renderer's row is draggable, carrying the workflow's address through it
  (`renderer(/…)`), so the level can be opened in a panel instead, through a renderer or `invocation`
  (`DefaultInvocationPanel`: its status and provenance, its inputs, a workflow's runs with their
  progress, and its output, the inputs and output as their entities).
- **Pages** (`fxtr-view`'s `declarePage` and `page`, the `PagePanel` slot): views of no location,
  opened in the detail view at `name()` by the links to them (the rail does not list them), handed
  the job's root invocation and their params. They fetch what they show themselves.

Nothing above loads a record: `RenderInvocation` dispatches on the name the host already has, and
the renderer loads through `useInvocation`. Records come from `views/invocation_source.ts`: a
workflow's from the live invocation entry (inputs are its `input` nodes, `params.name` →
`valueCid`; children are its run nodes with their spawns), a step's from the live step pin plus
the `StepRequest` entity behind `requestCid` (its `inputs` map is the spawn's sliced input arrays
by argument name), each watched while a renderer reads it. Complete records occupy separate live-store entries from overview metadata and load when a
renderer requests them. Arrays a renderer loads through `useEntity` come from the bytes endpoint;
ordinary cards and panels use server output pages.
- **What bypasses dispatch.** Array rows (`TablePanel`, `CellTree`) are this app's
  own, driven by the value's shape and the array's schema; only the entity ids inside them go
  through the view system. Inline tagged values (a `Message` inside a `Transcript`'s data) have no
  id and render as data.

## Run replay

On the Vite dev server, `App` lazily loads `debug/RunReplay` into the rail above the theme row;
`import.meta.env.DEV` is false in a production build, so the branch and the module are left out of
it. The replay replays the job on screen as if it were running, from a slider, play/pause, and a
speed, and can fail its busiest step at the current moment, for working on how progress is shown.

It works below the app: `api/graphql.ts` lets one `RequestMiddleware` answer every request in
the backend's place, and the replay installs one while it is on (`debug/replayData.ts`, whose
docstring lists what it answers and how). `debug/replay.ts` invents a timeline ending in each
level's recorded counts (every node present from the start, a run expanding once the runs
upstream of it finish, invocations running at the runner's default step limit) and places each
recorded invocation on it, so the overview, run pages, key counts, card tables (less the rows of
invocations not completed by then), step pins, complete invocation records, and the job summary
all agree at every point. The live store polls the replayed job like a real run; a seek calls
`pokeLive`, which polls at once. Each level of the job replays on its own timeline. Everything
read from the backend is kept until the replay stops, so it is meant for finished jobs.

## Theme

Light or dark follows the system's preference unless the **theme toggle** at the foot of the rail
(`ThemeToggle`, `lib/theme.ts`) picks one. A pick is `data-theme` on `<html>`, kept in
`localStorage` (`fxtr-theme`) and applied by the inline script in `index.html` before the first
paint. Every colour token in `styles/tokens.css` is written `light-dark(<light>, <dark>)`, and
`:root`'s `color-scheme` (`light dark`, or the picked one) chooses between them; the same property
themes native controls and scrollbars. Beside the viewer's own tokens are the chart colours,
a default palette for the data in renderers' charts (`--chart-*`: categorical series, a
sequential and a diverging scale); the comment above them says how they were checked. A change re-renders nothing: every rule, a project bundle's
stylesheet included, reads the tokens where it applies. An element that sets its own
`color-scheme` gets that theme's values inside it (a report can pin itself light this
way). The toggle is in the rail only, not in flowchart mode's floating controls.

## Not built

Persistence of the panel layout (it lives for as long as the page is open), or of which view shows
a node, a level, or an entity. Subscriptions (the backend serves GraphQL over HTTP only; this app polls
versions). A rollup of
a child workflow's progress into its parent's card (a child shows its own state; its subtree's
progress is seen by opening it). Call-site links for
run and source nodes. Values of derived nodes (the backend exposes none). A persistent row index for cold global table queries. Array blobs still decode whole on first server access. Dispatch for values that are not entities
(inline tagged dataclasses, TypedDict structs): they render by shape. Finish times per invocation in the overview: the heartbeat
spreads each poll's finishes across its interval, and knows nothing from before the page opened.

## Query regression checks

`test/live.test.ts` verifies summary-only loading and polling with a synthetic 100,000-spawn run.
`test/browse.test.tsx` checks page replacement, dimension picks/search/page preservation,
compatibility, the key column headers' click toggle, the slice panel's sources, cancellation, refresh coalescing, retry preservation, and batched metadata reads.
`test/refresh.test.tsx` exercises a slow custom renderer during live updates;
`test/values.test.ts` and the Python output tests share CBOR fixtures in
`test/fixtures/table-values.json` to check display and filter text across runtimes;
`test/invocation-pane.test.tsx` checks how a node's panel shows one failed, running, or finished invocation;
`test/invocationPanel.test.tsx` checks the `invocation` panel (status line, inline output, a
sketch while running, the error when failed, folded inputs, and Show all);
`test/jobs.test.tsx` checks stable initial job selection while new jobs arrive;
`test/events.test.ts` checks the events socket (its address, its listeners, reopening it with
backoff, and closing it for good); `test/launches.test.tsx` checks following launches (switching
with a toast and going back, a picked job kept across a reload, quick launches updating one
toast, and refreshing a resumed job already selected without navigating or raising a toast);
`test/liveViews.test.tsx` checks swapping in a rebuilt bundle (the page's own state kept), a build
that does not load, a failed build, and loading only the latest of the builds announced during a
load. `test/registry.test.ts` checks asset completion and failure ordering, inactive styles until
commit, expired-build recovery, and stalled-load cleanup;
`test/cardTable.test.tsx` checks a card's table (paging, which columns filter, picking and
platform-modifier row clicks, the last page's padding, its failed invocations and their errors), which
card a slice change holds still, and the column sizing. `test/address.test.ts` checks the address
syntax, relative locations, and the parts a head draws; `test/columns.test.tsx` the panels'
sequence (following, pinning, history, closing, moves and drops, default views, views declared
within others, the entity chip chain) and resolution across the job (a step inside a mapped
workflow, an unbound key, a level loading, panels kept when the centre moves to another level).
`test/flowchart.test.tsx` checks platform-specific wheel shortcuts and trackpad pinch zoom.
Model, progress, and graph-layout tests cover the remaining pure transformations.
