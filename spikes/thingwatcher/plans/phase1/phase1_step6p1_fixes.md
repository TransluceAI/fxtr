# Requested changes

- make sure gql tests pass with new transport

- graph layout issues
    - the nodes end up stuck on a weird diagonal line during initial layout
    - when adding new nodes, they appear in strange places and sometimes overlap existing nodes, maybe we should re-run layout of all nodes

- thing states
    - old thing states show the T badge, but aren't actually triggerable unless this is the active tick; maybe this should be grayed out for non-live ticks
    - deleted nodes aren't shown in the graph, they should show as dotted red outlines
    - deleted nodes aren't shown in the things list either

- details appears to be stale and not updating properly with the selection?
    - when I trigger, a node gets selected in the things list and the graph, but doesn't appear in details
    - also if I deselect in graph, it deselects in list, but stays selected in details.

- no trigger record shown in the UI yet. it would be great to show trigger records under the data in the details view, on the tick where the trigger happened. it would also be great if nodes that have trigger records get maybe a blue T badge in the things list to indicate they were triggered

- (new issue) missing nodes and duplicates of other nodes: i'm seeing waitpoints that link to e.g. unknown worker 3, and then 5 copies of worker 5 in the things UI and no worker 3. maybe this is something else.

- more prominent change labels in things list: maybe a +, -, or M badge that indicates (added, removed, modified), near the T badge.

- timeline
    - show tick markers as actual ticks on the timeline
    - color them by type, and also mark with a dot under it if we triggered something at this tick
    - add timeline level filtering
    - make it so that live mode shows up as a separate step in the slider, e.g. there's a part of the slider on the right that is past every tick, which represents the live streaming position.

---

## Fix Plans

### #1: GQL tests with new transport

**Problem**: The subscription test (`test_new_tick_subscription`) fails after we switched the server from `GraphQLWSHandler` (legacy `graphql-ws` protocol) to `GraphQLTransportWSHandler` (modern `graphql-transport-ws` protocol).

**Root cause**: The test uses `WebsocketsTransport.APOLLO_SUBPROTOCOL` which is the legacy protocol.

**Fix**: Change the test to use `WebsocketsTransport.GRAPHQLWS_SUBPROTOCOL` instead.

**Solution**: The gql Python library (v4.0) has issues with the `graphql-transport-ws` protocol despite advertising support. Rewrote the test to use raw `websockets` library instead, which works correctly.

**Changes**:
- `tests/test_graphql.py`: Replaced gql-based subscription test with raw websockets implementation that follows the graphql-transport-ws protocol directly (connection_init → connection_ack → subscribe → next)

**Status**: ✅ Complete - all 24 GraphQL tests pass

### #2: Graph layout issues

**Problems**:
1. Nodes end up stuck on a weird diagonal line during initial layout
2. When adding new nodes, they appear in strange places and sometimes overlap existing nodes

**Root cause analysis**:
- The fcose layout with `randomize: false` may be placing nodes deterministically in a line when they have no existing positions
- When new nodes are added, only those new nodes get positioned by the layout, but they may not have good starting positions

**Proposed fix**:
1. **Initial layout**: Set `randomize: true` for the initial layout when all nodes are new (no existing positions to preserve)
2. **Incremental layout**: When adding new nodes to an existing graph, either:
   - Option A: Re-run full layout on all nodes (loses existing positions but ensures good layout)
   - Option B: Run layout only on new nodes with constraints to avoid existing ones
   - Option C: Give new nodes random initial positions near center before running layout

**Recommended approach**:
- Track whether this is the "first layout" (graph was empty)
- For first layout: use `randomize: true`
- For incremental updates: use `fixedNodeConstraint` to pin existing nodes, only layout new ones

**Additional fcose tweaks** (from README):
- `nodeSeparation`: increase from 75 to ~100 to prevent overlaps
- `packComponents: true`: pack disconnected components nicely
- `quality: "default"`: keep default (good balance of speed/quality)

**Files to modify**:
- `thingwatcher/frontend/components/Graph/Graph.tsx`

**Status**: ✅ Complete - initial layout uses randomize, incremental uses fixedNodeConstraint

### #3: Thing states (T badge, deleted nodes)

**Problems**:
1. Old thing states show the T (trigger) badge, but aren't actually triggerable unless in live mode - should be grayed out for non-live ticks
2. Deleted nodes aren't shown in the graph - should show as dotted red outlines
3. Deleted nodes aren't shown in the things list either

**Analysis**:
- The T badge is in ThingsList - needs `isLive` prop to gray it out
- Deleted nodes come from `changes` with `kind: 'REMOVED'` - these things exist in the previous tick but not current
- Currently we only render `things` (current tick's things), not removed ones

**Proposed fix**:

1. **T badge graying**: Pass `isLive` to ThingsList, style T badge differently when not live (gray background, reduced opacity)

2. **Deleted nodes in graph**:
   - Merge removed things into the display list
   - Already have CSS for `node.removed` (dotted red border, opacity 0.5)
   - Need to include removed things in the `things` array passed to Graph, or handle separately

3. **Deleted nodes in list**:
   - Similarly include removed things in ThingsList
   - Already have CSS for `.removed` items (opacity 0.5, dashed border)

**Implementation approach**:
- In App.tsx, compute a merged list that includes both current things AND removed things from changes
- Pass `isLive` to ThingsList
- Update ThingsList to gray out T badge when not live

**Files to modify**:
- `thingwatcher/frontend/App.tsx` - merge removed things, pass isLive
- `thingwatcher/frontend/components/ThingsList/ThingsList.tsx` - gray T badge
- `thingwatcher/frontend/graphql/queries.ts` - expand changes.thing to include full data

**Status**: ✅ Complete

### #4: Details panel stale/sync

**Problems**:
1. When triggering, a node gets selected in things list and graph, but doesn't appear in details
2. When deselecting in graph, it deselects in list, but stays selected in details

**Analysis**:
Looking at App.tsx:
- `selectedThingId` is in app state, controlled by `setSelectedThing`
- Graph and ThingsList both call `onSelectThing` → `setSelectedThing`
- DetailsPanel receives `thing={selectedThing}` which comes from a separate GraphQL query
- The query uses `state.selectedThingId` as variable

The issue is likely that:
1. When selection changes, urql may be returning cached/stale data
2. When deselecting (null), the old `selectedThing` data may persist until query completes
3. The query has `pause: state.selectedThingId === null` but result may still hold old data

**Proposed fix**:
1. When `selectedThingId` is null, explicitly pass `null` to DetailsPanel (not stale query result)
2. Consider adding `requestPolicy: 'cache-and-network'` for fresher data
3. Ensure the query result is properly cleared when selection changes

**Files to modify**:
- `thingwatcher/frontend/App.tsx`

**Changes**:
- Added `requestPolicy: 'cache-and-network'` to thing details query
- Explicitly return null when `selectedThingId` is null (don't show stale data)

**Status**: ✅ Complete

### #5: Trigger records in UI

**Problem**: No trigger records shown in the UI. Would be nice to:
1. Show trigger records under the data in details view, on the tick where the trigger happened
2. Show a blue "T" badge on things in ThingsList that have trigger records

**Analysis**:
- Trigger records are available via `triggerRecords` GraphQL query
- Each trigger record has: `id`, `thing` (with shortId), `payload`
- Need to fetch trigger records for the current tick and display them

**Proposed implementation**:

1. **Query trigger records**: Add a query to fetch trigger records, possibly filtered by tick or thing
2. **DetailsPanel**: Show trigger records section when viewing a thing that was triggered at this tick
3. **ThingsList badge**: Add a blue "T" badge (different from red triggerable badge) for things with trigger records

**Approach** (confirmed):
- In DetailsPanel: Show trigger records for this thing at the current tick only
- In ThingsList: Show blue badge if thing was triggered at current tick only
- Scrubbing backwards lets you see old triggers at those ticks

**Files modified**:
- `thingwatcher/frontend/graphql/queries.ts` - added GET_TRIGGER_RECORDS query
- `thingwatcher/frontend/App.tsx` - fetch trigger records, filter by current tick, pass to components
- `thingwatcher/frontend/components/DetailsPanel/DetailsPanel.tsx` - display trigger records with blue styling
- `thingwatcher/frontend/components/ThingsList/ThingsList.tsx` - blue triggered badge
- `thingwatcher/frontend/types/index.ts` - added TriggerRecord type
- `thingwatcher/frontend/components/ThingsList/ThingsList.module.css` - added triggeredBadge styles

**Status**: ✅ Complete - trigger records now appear immediately after triggering. Fixed urql cache issue by passing `onTrigger` callback from App.tsx to DetailsPanel that calls `reexecuteTriggerRecords({ requestPolicy: 'network-only' })` after successful mutation.

### #5b: Missing/duplicate nodes issue

**Problem**: User reported seeing duplicate nodes (5 copies of worker 5) and missing nodes (no worker 3) in the UI.

**Investigation**:
- Confirmed via graphqurl that backend was returning duplicate rows with same shortId and key
- Added debug logging to App.tsx and Graph.tsx
- Attempted to inspect database directly but issue was with in-memory DB

**Current status**: ❓ Not reproducible - after restarting server with fresh database, the issue does not appear. May have been corrupted state from earlier debugging. Will revisit if it recurs.

### #6: Change labels (+/-/M badges)

**Problem**: The current change indication in ThingsList uses a subtle left border color (green/yellow/red). This is easy to miss when scanning the list.

**Proposed fix**: Add explicit +/-/M badges near the existing T badge:
- `+` badge (green) for ADDED things
- `M` badge (yellow) for MODIFIED things
- `-` badge (red) for REMOVED things

**Current state**: The ThingsList component already has `changeMap` (shortId → ChangeKind) and `getChangeClass()` for the left border. Badge styling exists for `triggerBadge` and `triggeredBadge`.

**Layout**: Badges appear after thingType, before T badges:
```
#{shortId}  {thingType}  [+/-/M] [T]
```

**Implementation**:

1. Add CSS styles in `ThingsList.module.css`:
   - `.addedBadge`: green background (#22c55e) with `+`
   - `.modifiedBadge`: yellow background (#eab308) with `M`
   - `.removedBadge`: red background (#ef4444) with `-`
   - Follow existing badge pattern (padding: 2px 6px, border-radius: 3px, font-size: 0.7rem, font-weight: 600)

2. Add rendering in `ThingsList.tsx`:
   - Check `changeMap.get(thing.shortId)` for each item
   - Render appropriate badge before the T badges

3. Keep existing left border colors for additional visual cue

**Files to modify**:
- `thingwatcher/frontend/components/ThingsList/ThingsList.module.css`
- `thingwatcher/frontend/components/ThingsList/ThingsList.tsx`

**Status**: ✅ Complete

### #7: Timeline improvements

**Problems**:
1. The slider shows ticks as a continuous range, but doesn't show individual tick markers
2. No visual indication of tick type/level or whether triggers occurred
3. Level filtering checkbox exists but isn't wired up (has TODO comment)
4. Live mode is conflated with the latest tick position - should be a distinct "past all ticks" position

**Current state**:
- Timeline receives `ticks: Tick[]`, `latestTick: Tick | null`
- Slider is a standard `<input type="range">` from 1 to latestTick.index
- Level filter checkboxes render but have empty onChange handler
- App.tsx has `allTriggerRecords` with `triggerTick` field available

**Proposed implementation**:

#### 7a. Tick markers on timeline

Replace the plain slider with a custom tick track that shows each tick as a small mark:
- Render a container with positioned tick markers
- Each marker is a small vertical line or dot
- Clicking a marker or dragging selects that tick
- Keep slider thumb for smooth dragging

#### 7b. Color ticks by level + trigger dot

- Color each tick marker by its `level` field (reuse existing level colors from CSS)
- Add a small dot below ticks where `triggerTick` matches (need to pass trigger records to Timeline)

Data flow change:
- Pass `triggerRecords` (or just `triggeredTicks: Set<number>`) from App.tsx to Timeline

#### 7c. Level filtering

Wire up the existing level filter checkboxes:
- When a level is unchecked, hide ticks of that level from the track
- Filter affects which ticks are navigable via prev/next buttons
- Store filtered tick indices for navigation

Context change:
- `setLevelFilter` already exists in AppContext
- Need to use `state.levelFilter` to filter displayed ticks

#### 7d. Live mode as separate step

- Extend slider max to `latestTick.index + 1`
- Position `latestTick.index + 1` represents "live" mode (past all ticks)
- Show a distinct "LIVE" marker at the end of the track
- When slider is at this position, display live streaming behavior

**Implementation order**:

1. [x] Pass `triggeredTicks: Set<number>` from App.tsx to Timeline
2. [x] Implement custom tick track with markers (replace plain slider)
3. [x] Color markers by level using existing CSS variables
4. [x] Add trigger indicator dots below markers
5. [x] Wire up level filtering (filter displayed markers + navigation)
6. [x] Add live mode as +1 position with distinct marker

**Additional fixes**:
- Fixed slider/tick alignment (off-by-one) by using consistent positioning formula
- Fixed slider drag control loss by tracking drag state locally and not disabling during drag
- Fixed gaps when filtering: filtered ticks are now positioned contiguously across the track, slider maps positions to filtered tick indices

**Files modified**:
- `thingwatcher/frontend/App.tsx` - compute triggeredTicks set, pass to Timeline
- `thingwatcher/frontend/components/Timeline/Timeline.tsx` - custom tick track with markers, level filtering, live marker, drag state management, filtered tick positioning
- `thingwatcher/frontend/components/Timeline/Timeline.module.css` - marker styles with level colors, trigger dots row

**Status**: ✅ Complete

### #8: Graph node spawn positions

**Problem**: With fixed node constraints disabled, new nodes spawn at unpredictable positions which looks jarring, especially when scrubbing through ticks.

**Proposed fix**: Set initial positions for new nodes before running layout:
1. **Connected nodes**: Spawn at the average position of all connected nodes (both incoming and outgoing edges)
2. **Orphan nodes**: Spawn at a deterministic "random" position using shortId as a seed, so scrubbing always places them in the same initial spot

**Implementation**:
- Added `seededRandom()` function using mulberry32 algorithm
- Before adding new nodes, build edge maps (outgoing/incoming) from all edges
- For each new node:
  - Look up connected existing nodes via edge maps
  - If connected: set position to average of connected existing nodes
  - If orphan: use seeded RNG based on shortId to place within viewport bounds
- Set `nodeData.position = { x, y }` before `cy.add()`

**Files modified**:
- `thingwatcher/frontend/components/Graph/Graph.tsx`

**Status**: ✅ Complete

### #9: Prevent zoom-in when nodes are removed

**Problem**: When scrubbing back to ticks with fewer nodes, the layout's `fit: true` causes distracting zoom-in as it tries to fill the viewport with the remaining nodes.

**Possible approaches**:

1. **Conditional fit**: Only use `fit: true` when adding nodes, use `fit: false` when only removing
   - Pro: Simple, targeted fix
   - Con: Graph might drift if nodes are removed from one side

2. **Set maxZoom on cytoscape instance**: Cap how far in it can zoom (e.g., `maxZoom: 2`)
   - Pro: Global protection, simple
   - Con: Also limits manual zoom-in if user wants to inspect details

3. **Clamp zoom after layout**: After layout completes, check if zoom exceeds a threshold and reset
   - Pro: Only affects auto-fit, not manual zoom
   - Con: Slightly more complex, may cause visual jump

4. **Remember and restore zoom**: Capture zoom before layout, restore after (with `fit: false`)
   - Pro: Smooth, preserves user's view
   - Con: May cause nodes to go off-screen if graph shrinks significantly

5. **Use padding-based minimum bounds**: Increase `padding` option significantly so fit doesn't zoom in as much
   - Pro: Simple tweak
   - Con: Less precise control

**Chosen approach**:
- Disable layout animation for simplicity
- After layout, identify changed nodes (added/modified/removed)
- Check if all changed nodes are already visible in viewport
- If not all visible, fit to changed nodes with zoom clamping (e.g., max 1.5)
- Use `cy.extent()` to get viewport bounds, compare with nodes' bounding boxes

**Implementation**:
1. Save original node positions and viewport state before layout
2. Run layout with `animate: false`, `fit: false`
3. Collect changed nodes from `changeMap` (ADDED/MODIFIED/REMOVED)
4. Check visibility based on final positions vs `cy.extent()` viewport bounds
5. If any off-screen, temporarily fit to calculate target zoom/pan, clamp zoom to max 1.5
6. Restore original positions and viewport
7. Animate nodes and viewport in parallel using `node.animate()` and `cy.animate()` with `queue: false`

**Files modified**:
- `thingwatcher/frontend/components/Graph/Graph.tsx`

**Status**: ✅ Complete
