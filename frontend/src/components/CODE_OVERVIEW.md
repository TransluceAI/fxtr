# frontend components — code overview

What the experiment viewer draws, and the components in this directory that draw it. The app's
structure, its data layer (`api/`, `model/`, `lib/`), the detail view's panels and addresses, and the view
system are in the parent overview, `frontend/CODE_OVERVIEW.md`.

## What the UI shows

- **Keyboard help** (`KeyboardShortcuts`) opens with Command+, on Mac or Ctrl+, on Windows/Linux,
  anywhere in the IDE, including inputs and open dialogs. `App` mounts it outside job loading and error
  boundaries. Its searchable reference groups bindings by context and describes hover and
  focus requirements, showing only the current platform’s modifier; keep its entries aligned
  with the controls that implement them. `lib/keyboard.ts` owns modifier matching and labels.
  The modal contains keyboard focus and events while open, then restores the previous focus.
  `test/keyboardShortcuts.test.tsx` covers global access, dismissal, and context isolation.
- **A job** (chosen in the picker above the minimap; the selection is kept in the URL hash) is one
  request to run a root workflow. Its root invocation is the first **level**.
- **A level** is one workflow invocation's DAG. A header at the top gives the invocation's name,
  pathname, state, timing, attempt count and error, then one card per node that was registered or
  run: `input` and `source_array` nodes are `source` cards, `run_step` nodes `step` cards, and
  `run_child_workflow` nodes `workflow` cards. A card shows its function's code docs, then its args
  below them. Derived nodes (`combine`, `stack`, `query`) have
  no stored value and are not cards: they appear inside the **args** of the runs that read them,
  as the expression that built the argument (`combine({a: …, b: …})`; a query node, whose plan is
  stored encoded, as `query(<its inputs>)`), with a chip for each card referenced, listing that card's dims in neutral text (a chip short of
  room wraps inside itself). The chip
  shows neither the consuming run's mapping nor the level's slice; its hover preview says which
  dims each invocation sees only its own key's rows of. Dim-kind colour
  (mapped warm, replica green) always describes the card it is drawn on, never an upstream one.
  The minimap draws those references as the dependency graph. Hovering a chip turns its step's
  minimap row blue, and when the step's card is fully on screen lights that card up in place of
  the chip's hover preview (`lib/cardSpotlight.ts`): a blue glow fades in around it, both pulsing
  gently in step while the chip stays hovered. Clicking a chip (or anything else naming a step:
  the return card's chips, the level header's activity links) jumps the trace to its card, which
  glows with the same ring and glow, held for a moment and then faded out (`jumpGlow` on
  `StepCard`, replayed by each jump). These are the only blue a whole card (or minimap row)
  takes: neither selecting nor hovering a card outlines it. A card with no function and no arguments (a registered array) omits the docs/args block.
- **The return card** closes the level: `returned` followed by the expression the workflow
  returned, whose chips select the cards they name. When a derived node was returned, no card holds
  its value, so the return card's head has an `open` button opening its panel.
- **A run's invocations** are its spawns: one per mapped key and replica key. The **slice** is
  one per level: the value picked for each dim sliced on, keyed by dim name (`lib/levelState.ts`),
  named after fxtr's `Array.sel` (the slice at the given dim values, with those dims removed).
  Picking `model=X` in any card or the slice panel slices every card and panel with a `model` dim
  at it, and a level change clears it. A slice applies to every node with the dim, whether or not
  the node maps over it: a run's invocations are narrowed by the slice of its invocation dims
  (`invocationSliceFor`), and a node's rows by the slice of any of its dims (`rowSliceFor`; a dim
  produced inside an invocation is sliced as the server reads the arrays).
- **What a card lists** (`CardTable`) closes each card with output (a run with invocations, a
  registered array, the return card's derived value), under its args (`CardTableState.mode`). A
  run whose invocations each produce one row lists its invocations, whose table holds those rows'
  values too. A run whose invocations produce several rows each has a segmented control
  (`invocations` | `data`, `data` to begin with) choosing what, floating just above the table's top
  edge at its right, in front of the args rather than in a row of its own. It has a mode chosen
  exactly while the table is shown (the chevron badge below): choosing the mode shown hides the
  table, and choosing any mode while it is hidden shows it in that mode. A node that was not run
  lists its rows.
  - **`data`**: the node's rows in the level's slice (on every dim of its output, `rowSliceFor`),
    ten a page, every column (the key columns of sliced dims too, each holding its one value).
    Every column sorts; only value columns filter (key columns are narrowed by slicing). Clicking a
    row opens the invocation that produced it at the row's keys (`invocationTarget` in
    `lib/linkBase.ts`), so the `invocation` panel's output is that one row, with Show all for the
    rest (a node that was not run opens its own panel at the row's keys, `nodeTarget`), the row
    lit in selection blue while that panel is open (only it: a row is never lit for keys that
    merely match ones picked elsewhere; a node's panel's table lights its rows the same way), and
    clicking one with the platform's primary modifier (Command on Mac, Control elsewhere:
    `lib/keyboard.ts`) slices the level at its keys (while it is held, the rows show as clickable:
    `lib/modifierClick.ts`). At each row's right end (the trailing cell, under the column
    menu) the state of the invocation that produced it, when it has one to show: a spinner while
    it is queued or running, the invocation icon (`INVOCATION_ICON`, a receipt) in red once it
    failed; the page's invocations are read in one batched request (`useRowInvocations`,
    `fetchFirstSpawns`).
  - **`invocations`**: a row per invocation in the slice (`useInvocationPage`, over the backend's
    `invocationPage`, paged on its own): its keys, its state (the backend's display state), then
    what it produced, which comes with it: its one row's values when every invocation produces one
    row (the output has no dim but its invocation dims), or else how many rows, in one column with
    no header, faint and italic since it describes the rows rather than holding data
    (`Column.describes`). At its right end, a spinner while it is queued or running. Every column
    sorts and every column but the keys filters, as in the data table, over every invocation in
    the slice (the server's work), with filters and an order of the invocations' own
    (`CardTableState.invocationFilters`, `invocationSort`); an invocation with no value sorts last.
    Clicking a row opens the invocation's panel, and the row is lit while the panel shows it;
    double-clicking a child workflow's row shows that invocation's level in the trace. A
    failed invocation's row is its keys, then one light red stretch with dark red text across the
    rest of the row holding its error on one line (`InvocationFailure`, from the step's pin or the
    child's overview); it is left out when the value columns are sized.
  - **`keys`** (`CardKeys`; not offered by the control for now): a column per invocation dim,
    each its values in the slice
    (`KeyValueList`), headed by the dim (coloured by kind) and its value count. The columns pick
    one invocation together (`CardTableState.keysPick`): choosing a value in one picks a value in
    every column, keeping the others' picks where an invocation has them all and otherwise taking
    the first invocation with the chosen value, then opens the node's panel at that invocation's
    keys. The picked values are drawn in selection blue (`KeyValueList`'s `selection`), since they
    name what the panel shows rather than a slice. Values the other picks rule out are dimmed but
    can be chosen.

  While a table has more than one page it has a pager, and every page is ten rows tall (the last
  one padded), so paging does not move the cards below; a table of one page (fewer rows, or
  filtered down to them) has no pager and is as tall as its rows. A **chevron badge** centred on
  the card's bottom border, shown while the card is hovered (down to show, up to hide), shows or
  hides a table's rows, or in `keys` the value lists under the columns' heads (shown until toggled); the header row stays either way, so the key columns'
  headers slice the level with the rows hidden (no column then draws sort or filter controls), and
  the pager shows only with the rows. Sliced on every dim of its output with the rows hidden, the
  data table's one row's values show in the value columns' headers, on a second line under each
  name like a key column's picked value (an entity link slimmed to that line), so they still read;
  otherwise (several rows, or the row shown) that line names the column's type in faint text
  (`schemaLabel`: `str`, `struct`, an entity's short type name), as an unpicked key column's header
  names its kind. What the card lists, the table's filters, order, pages and whether its rows are
  shown are the card's table state (`CardTableState` in `lib/levelState.ts`, read through
  `lib/cardTable.ts`'s `useCardTable`), kept per slice: another slice starts at its first pages
  with nothing picked, keeping the rest.
  The card's **failed count** lists the failed invocations alone (`failedOnly`), switching the card
  to `invocations`, and draws the count in inverted colours (red ground) until clicked again, which
  lists every invocation again; switching the card's mode clears it.
  The **key columns' headers** are the node's slice control, coloured by kind (mapped warm, replica
  green, a dim produced inside an invocation neutral), each naming its dim, its value count, and on a
  second line the value picked for it (a chip in the header's colours inverted, so a sliced column
  stands out), or while none is the dim's kind (`int` or `str`) in faint text, with a sort button at its right. Clicking one floats the value columns of every key column
  below the header row, each under its own column's header, over the key columns' rows (the value
  columns stay uncovered) and the cards that follow (the page does not move), until a
  header is clicked again, a click lands outside the table, or Escape is pressed. Each column is a
  searchable list of the dim's first 1000 matching values, only those in view drawn, ending in
  "more not shown" when more match (`KeyValueList`: a dim that keys the run's
  invocations from its spawn table, any other from the node's output, `nodeDim`), the ones the
  other picks rule out dimmed and not choosable; clicking the picked value again clears its dim, as
  does the `clear` cell heading the column of a picked dim (in the search box's place and at its
  height, so picking or clearing never moves the values, and outside the scrolling values, so it
  stays put; the search still applies while hidden); a pick the node has no invocation for (made on
  another card) is struck through. Where the values float past the card's foot, they read as the
  card extending: they continue its border and round their bottom corners, and the card's bottom
  corners they meet go square. The flowchart's selected card has the same table.
- **Column widths.** `lib/columnWidths.ts` sizes a card table's columns from one unit, a sixth of
  the table and at least 100px (the same in every card, since they are as wide as each other): a
  key column is one unit, so a card's key block counts its dims against every other card's; a
  value column is as wide as its header and cells need (between 60px and three units), measured
  once from the first page with rows so paging does not move it. Room left over is shared by the
  value columns still cut off, or else left empty at the right end; a wider table scrolls
  sideways on its own. A column resizes freely, snapping to its default width within 14px of it,
  and while one wider than its default is dragged a dashed line marks that width
  (`components/useColumnWidths.ts` measures, and holds the resizes). Key column headers are cut
  off at their edge.
- **Holding a card still.** Every slice change changes the height of every card's table, so the
  trace holds one card at its place on screen while they settle (`lib/scrollAnchor.ts`, through
  `setSlice`'s `anchor`): the card a pick was made in (a key column's values, a row's modifier-click,
  the failed-count picker), or for a pick made elsewhere (the slice panel, a panel) the first card
  whose top is on screen, or failing that the card across the top of the viewport. Each time a
  card or the level changes size the scroller is moved to put it back before the change is
  painted; the hold ends when the trace is scrolled by hand, another pick is made, or after 10s.
- **The slice panel** (`SliceDims`) floats at the top right of the trace (sticky as it scrolls;
  the level leaves room for it) and of the flowchart, not over a custom view: a button with a
  sliders icon saying `slice dims`, or `sliced on N dims` while the slice is on N dims, with an ×
  joined to its right then that clears the slice in one click.
  It opens a 500px-tall panel of two columns: the left lists every dim of the level's nodes in
  order of first appearance, each with its picked value (or nothing) under its name, none coloured
  by kind (mapped is a property of a node, not a dim), with a `clear all` button pinned at its
  foot while the slice is on any dim; the right is the `KeyValueList` of the dim
  chosen on the left. Its values come from one node (`levelDims` in `lib/keyLists.ts`): the first
  run the dim keys the invocations of (the spawn table's `keyPage`), else the first node whose
  output has it (`outputDimValues`, a scan of its arrays). Values the level's other picks rule out
  on that node are dimmed but can still be chosen. A click outside, Escape, or the button closes it.
- **The views button.** A step's views of the project's own (its function's `InvocationPanel`
  renderers, `isProjectRenderer`) are opened by the button in the card's head, after its status
  (`ViewsButton` in `PanelButton.tsx`): the view's name after the **custom-view icon** (lucide `Component`,
  `CUSTOM_VIEW_ICON`, also in the view dropdown's count badge) for one; for several, two segments,
  the current view's (the first until another is chosen) and one with their count and a chevron,
  whose menu lists them all, choosing one opening it and making it the current view; and none for
  a step without any or that was not run. Each opens the node's panel at `<view>(/…)` from the
  trace; click it, or drag it into the columns. A segment is lit while a panel shows the node through a
  view it stands for (`viewsShownOf`), not through `invocation`, its output, or a view declared
  within another. The node's other panels open from its rows. Full output is the panel with no key
  picked.
- **Breadcrumbs and views.** The breadcrumbs above the minimap list every level opened, root first, with the one on screen in a blue box. Clicking a
  higher breadcrumb shows that level and keeps the deeper breadcrumbs for a quick return; they are
  dropped when a different child is opened from above them, or when a breadcrumb is double-clicked.
  The rows sit under a `trace` heading and take their own height; when the workflow on screen has
  invocation renderers of its own, they are listed right below the rows under a **custom views**
  heading. Choosing a renderer shows the whole invocation through it in place of the header and
  cards, the detail view staying beside it and the minimap showing no selection; choosing it
  again, or clicking a row in the minimap, returns to the trace. A renderer's row can be dragged into the detail view to open the
  invocation there. Under them, the project's **pages** (`page:name`), each opening in the detail
  view from the rail.
- **The detail view** on the right is columns of **panels**, each showing an address (the parent
  overview's "The detail view: panels and addresses"). `DetailPane` draws one: its head
  (`PanelHead`) and the body of its target, or why it has none. The head is one line that is the
  address, typeset: the path's parts, those a browsing panel shares with the address of the panel before it dim and giving way
  first when narrow, with an icon before the part naming what the panel shows (the custom-view
  icon, `CUSTOM_VIEW_ICON`, before the last part of a page or a view through a renderer of the
  project's own, `isProjectRendererId`; the step's kind, `KIND_ICONS`, for a node; an invocation's receipt, `INVOCATION_ICON`; an entity's isometric cube,
  `ENTITY_ICON`, in `lib/kindIcons.ts`; a file for a function's code); the panel's own params as gray chips (wrapping onto a line of
  their own under the path when they do not fit beside it); a count of those from the slice
  (`slice n`), those inherited from the panel before it left out; the pin (filled violet on a pinned
  panel; on a browsing one, a button shown only while the pointer is over it), back, forward, the view switcher, close. A pinned panel's head has a faint violet wash (`--pinned-bg`); a browsing panel's is the panel's own light grey. Hovering the path or the
  count opens the address card, which says what the icon means, its path as the panel stores it, and where each
  binding comes from.
- **Code.** A card's function, and the level's workflow in its header (`LevelHeader`), show their
  docstring and an "implemented" chip (`SourceDocs`, whose `codeAt` is the node's step path: the
  card's `[...levelPath, step.name]`, the header's `levelPath`; an argument's hover preview shows
  the docs with no chip). The chip is a link to the `code` view (`code(/step)`, or `code(/)` for
  the root workflow): clicking follows it (Alt pins), hovering lights where it would open, dragging carries
  it into the columns, and it is lit like an entity chip while its panel is open. The panel
  (`CodeBody`) shows the function's file at the commit the job ran, its lines marked, in a box of
  its own capped at 600px that scrolls itself to the function's first line; its head has the
  split "Open" button (`OpenInEditor`), whose menu chooses the editor (`lib/editor.ts`).
- **A node's panel** (`StepPanel`, a panel at a node's address) is fixed to a node, not an
  invocation: the output of the invocations its bindings pick out of it (every row of a node that
  was not run), sliced at their values of its other dims too. The panel's head has a dropdown
  (`StepControls`, a `ViewSelect`: the parent overview's "The view system") choosing how it shows
  them, which sets the view in the panel's address (`table`, `<renderer>`); a panel showing a
  view of the project's own (`isCustomView`) or a renderer of the step's function has none, and is
  switched from the step card's views button, as the level's own panel through a renderer is from
  the rail. It offers the entity renderers of the output's type; `output (raw)` is the fallback a step
  without a renderer of the project's shows, and what `table` names, listed only while it is the
  view shown (an invocation's own panel shows its output). What a panel can show:
  - `output (raw)`: when the slice picks out several rows, every row as one table (a row picked opens the
    panel's address at its keys, from the panel), with the key columns of the dims sliced on left out and no head line
    naming the table and its type (the panel's head names the node; the column menu sits at the
    right end of the column header row instead); when it picks
    out one, that row's value as a data tree (an entity decoded, through the `EntityPanel` slot's
    data tree). Rows a change of slice brings in show at once
    (only rows a job in progress adds fade in);
  - `output <renderer>`: each `EntityPanel` renderer of the output's entity type (only when every
    value is an entity), handed the one entity. When the slice picks out several rows, the panel
    stacks the table above the picked row's entity through the renderer (`RowSplit`; no draggable
    split, since a column's panels grow to their content): the table as in `output (raw)` but with
    a click picking a row (lit with `data-selected`; `PagedTable`'s `onSelectRow` and
    `selectedRowId` in place of `rowLink`) instead of following a link. The output's first row is
    picked until one is clicked, and again once the slice changes; paging, sorting and filtering
    keep the pick;
  - `invocation <renderer>`: each `InvocationPanel` renderer of the step, handed every spawn and
    the params it declares (a detail view among them, opened by a link from the view it is declared
    within).

  The head's view dropdown chooses only among the output and its entity type's renderers;
  invocation renderers are chosen from the step card's views button, and a panel showing a field
  path into the output (`.output…`) has no dropdown.

  `invocation` shows the one invocation the bindings pick out (`OneInvocation`, through
  `DefaultInvocationPanel`), top to bottom: a status line (`completed in 7s · <when>`) with its
  provenance under it as fine print (commit, source link, the job it was reused from, cache key);
  its inputs as folded rows (name, type, how many rows it was given), each expanding to the rows
  of its source node at the invocation's keys (`.inputs.<name>`; an input that is an expression shows
  its entity); and last its output, inline, as `.output`
  at the panel's params shows it (a value, or a table whose rows open `.output` at their keys), a
  sketch of its schema's fields while it is queued or running, or its error when it failed. When the
  panel binds dims produced inside the invocation, the output says which of its rows those leave
  (`1 of 2 rows · sample=0`) with **Show all**, which leaves them out of the panel's params
  (pinning the panel when it inherits them). The panel has no view dropdown or info button. With no view in its address, a panel shows the
  default (`defaultPanelView`): the step's first invocation
  renderer of the project's own, else the output type's first renderer of the project's own, else
  `output (raw)`; the viewer's default renderers never count. An output type's renderers are known once
  its first row's type is, so an entity output can switch to its renderer as that loads. Above an output the unfinished invocations are listed (each
  a button slicing at its key); a slice at one unfinished invocation shows its state and log lines
  instead (`PendingInvocation`). The head of any other view also has the `InvocationInfo` button when the slice
  leaves one invocation. In flowchart mode, clicking a node opens its panel.
- **Rows** of a run card are the rows every invocation produced, prefixed with the invocation's
  key, which is how the runner assembles a run node's value. Cells are decoded from the DRISL bytes
  the backend serves, so entity ids are `CID`s and bytes are `Uint8Array`s. Non-entity values
  render by their shape and the array's schema. Lists and structs inside grid cells show their
  contents on one line with an item/field count badge. The count stays visible while the contents
  use ellipsis to fit the remaining column width; resizing a column reveals more text.
  Their filters match both contents and item/field counts, while sorting uses the compact count
  summaries. A row that clicking opens in a panel (a card's or a node's panel's table) can also be
  dragged into the detail view to open it where it is dropped, and is read in full there; in any other grid (an array entity's) every row has an expand button, appearing on
  hover or keyboard focus, opening the complete row in a modal. Long strings in the detail view
  also have an expand button.
  A click in a grid focuses it; while it has focus, `j`/`k` open the next or previous row from the
  lit one, or, when an entity chip in it is lit (the browsing panel after its view shows it), click the
  next or previous chip in the same column, at the same place in its cell, passing over rows with
  none there (`lib/tableKeys.ts`). Past the page's last row (or first) they turn the page through
  the table's pager (`turnPage`) and, once the next page is on screen, open its first row, or click
  its first chip in the column (its last, going up).
  An entity id renders through the **view system** (the parent overview's "The view system"): a link labelled by the entity's type,
  a hover preview, and, when opened, a panel.

## Layout

```
components/
├── KeyboardShortcuts.tsx the global shortcut listener and searchable, context-labelled help dialog
├── PagedTable.tsx        current output pages and global filter/sort; shownPage, shared with
│                        CardTable
├── Pagination.tsx        editable row range, page snapping, and first/previous/next/last arrows
├── JobPicker.tsx        the job name + chevron above the minimap; searchable dropdown; each
│                        job led by its state glyph; opening it refetches the list
├── LiveIndicator.tsx    under the picker: "reconnecting…" after a failed poll
├── Minimap.tsx          breadcrumbs of levels, one row per card, the compact dependency graph,
│                        the viewport rectangle (fed by a ViewportStore the trace's scroller
│                        writes, so scrolling re-renders only it), rows lit through the
│                        spotlight store (so a hover re-renders only them), and the shared
│                        trace/flowchart mode controls
├── Flowchart.tsx        the full-level card DAG pane: compact node heads (kind icon and name),
│                        a run's progress line along its foot until it is all done, cards as narrow
│                        as their content, mapped/replica dim chips stacked down the card,
│                        parallax pan and zoom (primary-modifier scroll or trackpad pinch; physical
│                        Control-scroll on Mac pans), off-screen card counts at each rank's
│                        sides, and selected-card navigation
├── flowchartParallax.ts the flowchart's parallax pan: one pan fraction slides every rank
│                        sideways on its own (an overflowing rank scrolls end to end, a rank
│                        that fits stays put; a rank's width is its cards', and the lanes
│                        of edges passing beside them are squeezed into the room left on
│                        screen), with drags and zooms anchored on the overflowing rank
│                        nearest the pointer
├── flowchartLayout.ts   the flowchart's layered layout; its header lists what a good
│                        layout is here and the phases: ranks, slots (a single-consumer
│                        input beside its consumer), crossing-minimizing order, ports,
│                        L1 positions on a 10px grid (lib/networkSimplex), S-curve routes;
│                        cards grow with their dims, and a rank is as tall as its tallest.
│                        test/flowchartLayout.test.ts checks invariants and quality budgets
│                        over test/flowchartGraphs.ts: the cc-spoof and behavior-transfer
│                        production graphs, stress shapes, and seeded random families
├── LevelViews.tsx       below the minimap: the trace and the InvocationPanel renderers of
│                        the workflow on screen, as the views of the level
├── LevelHeader.tsx      the invocation on screen: glyph, name, pathname, the workflow's
│                        docstring and code chip (SourceDocs, when it has them), state,
│                        ticking duration, a summary of its runs' states, error, what the
│                        body is awaiting, and "jump to activity"
├── StepCard.tsx         a card: kind, name, a run's status (RunStatus) and progress line, the
│                        views button, the function with `-> <return
│                        type>` (typeLabel, in full on hover), docs (SourceDocs), args
│                        (ArgValue); then its table (CardTable), a run's mode switch
│                        (invocations, data, keys) floating above it
├── RunStatus.tsx        a run's status at the right of its card head: its percentage, or a
│                        word in its place, and failures or stopped invocations in
│                        words (the failed count shows the failed invocations in its table);
│                        the hover card
├── RunProgressCard.tsx  the hover card's body: the heartbeat, the legend
│                        with each state's count beside rate, time left, last finish and
│                        longest running, and a row per error type; or, once nothing is left
│                        to do, when it finished and how long it took
├── Heartbeat.tsx        a minute of a run's finishes as 2px blocks, stacked per column of time
│                        and sized to fill the height (bars once a column is too crowded),
│                        sliding a whole pixel at a time; heartbeatLayout
├── ProgressLine.tsx     a run's 3px progress line (HTML for a card head, SVG for a flowchart
│                        node), and the sheen sweeping its finished part on each update that
│                        brings finishes
├── RunningIndicator.tsx the pill over the trace naming the nearest running step while none is
│                        on screen, at the side it is on (it reads the ViewportStore itself);
│                        offscreenRunning, which step that is
├── PanelButton.tsx      card heads' buttons opening a panel (click or drag): `PanelButton`, and
│                        `ViewsButton`, a step's project views (one, or the current one's
│                        segment beside a counted one with a menu of several)
├── ReturnCard.tsx       the level's last card, always present: the returned expression, a
│                        button opening its value's panel and its table when no other card
│                        holds it, or why there is none yet
├── SourceDocs.tsx       docstring + "implemented" chip from the backend; the chip is a link to
│                        the `code` view of the node (`codeAt`)
├── CodePane.tsx         a `code` panel's body (CodeBody: a function's file at the job's
│                        commit, its lines marked, in its own scrolling box) and OpenInEditor,
│                        the head's split "Open" button, whose menu chooses the editor
├── ArgValue.tsx         an argument expression: chips with hover previews (or lighting up
│                        their card when it is fully on screen) and literals
├── KeyPill.tsx          a card's own invocation key for a dim, inline
├── CardTable.tsx        what a card lists in the level's slice: its rows or its invocations
│                        ten a page, rows shown or hidden under an always-shown header row,
│                        rows opening panels (primary-modifier-click slicing), key columns'
│                        headers that float the value columns (a KeyValueList per dim) below
│                        them on click; or CardKeys
├── CardKeys.tsx         a run's keys mode: a KeyValueList per invocation dim, picking one
│                        invocation across them all and opening its panel
├── KeyValueList.tsx     one dim's values: its first 1000 matching a search, virtualized, with
│                        compatibility, "more not shown", j/k, and a clear cell in
│                        the search box's place while a value is picked
├── SliceDims.tsx        the slice button and panel over the trace/flowchart: every dim of
│                        the level on the left, the chosen dim's values on the right
├── StepPanel.tsx        a node's panel: its output in the level's slice through the
│                        chosen view (a table of several rows or one row's value, an
│                        output or invocation renderer); OneInvocation, `invocation`'s
│                        values (output, inputs, Show all) for DefaultInvocationPanel;
│                        StepBody, what it shows around the output; StepControls, the
│                        panel head's view dropdown (a ViewSelect over panelViewOptions)
│                        + info
├── ViewSelect.tsx       the view dropdown every head uses (radix Select): "views" with a
│                        count of the project's views over the view shown; the menu lists
│                        the project's views under "from this project"
├── PendingInvocation.tsx an invocation not finished yet, or failed: state, elapsed time,
│                        error, and a step's latest ctx.log lines, current while shown
├── InvocationFailure.tsx a failed invocation's error on one line, for its table row
├── RendererSelect.tsx   a ViewSelect choosing the default view or a registered renderer
│                        (an opened entity, the level's invocation in a panel)
├── InvocationInfo.tsx   an invocation's status line (state, timing, error) and provenance
│                        (commit, source link, cache key or pathname), and the info button
│                        showing both
├── DetailPane.tsx       one panel: its head and the body of what its address shows (a node
│                        through a view, a value, an entity, the level, a page, the experiment,
│                        a function's code)
├── PanelHead.tsx        a panel's head: the address typeset, its params by source, its
│                        controls (its pin among them; "Open" in an editor on a code panel),
│                        and the address card
├── TablePanel.tsx       the sortable, filterable grid (a failed invocation's row: its keys,
│                        then its error across the value columns; each row's invocation at its right end;
│                        j/k from the lit row or chip)
├── useColumnWidths.ts   its column widths: defaults from measured content, resizes, snap
├── ValueView.tsx        a cell at full size (CellTree, entity ids as EntityLinks), FieldList
├── StateGlyph.tsx       the run-state glyph (GlyphShape inside an SVG, StateGlyph inline,
│                        QuietGlyphShape for SVGs outside the minimap), a run's progress
│                        arc, and the running spinner
├── KindLabel.tsx        a node kind as its icon and word
├── HoverPreview.tsx     every hover preview (entity chips, argument chips, a run's status):
│                        the pointer passes through the card, so leaving the trigger closes it
├── ExperimentReadme.tsx the rail's experiment link and the panel that renders the project's
│                        EXPERIMENT.md; both disappear when the project has none
├── ViewsNotice.tsx      the rail's note when the project's renderers did not load,
│                        are being rebuilt or did not build, or were just updated; failures are
│                        persistent alerts styled with the viewer's error colours
├── ThemeToggle.tsx      at the foot of the rail: follow the system theme, or pick light or
│                        dark
├── Toasts.tsx           sonner's toaster in the top right corner, its toasts unstyled and
│                        drawn as the viewer's floating cards (lib/launches.tsx raises them)
├── Status.tsx           Loading, Problem (a failed load with its backend), RetryLine (a
│                        failed read and its retry button)
└── ValueModal.tsx, Markdown.tsx, ErrorBoundary.tsx
```
