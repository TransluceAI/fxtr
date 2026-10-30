# Phase 2 Step 10.2 Post-2: Thing Deletion Display Adjustment

## Overview

Change how deleted/removed things are displayed in the UI. Instead of showing things as "REMOVED" in the tick where they no longer exist (which causes broken selection), show them as "DISAPPEARING" in the tick where they still exist but will be gone in the next tick.

## Problem

Currently:
- At tick N, if a thing was removed (existed at N-1 but not at N), it shows as REMOVED
- REMOVED things appear translucent with red dashed border
- **Bug**: Selecting a REMOVED thing fails because the thing doesn't exist at tick N - ThingPanelContent shows "Thing not found"

## Solution

Show things as "DISAPPEARING" in their **last tick of existence**:
- At tick N, look ahead to tick N+1's changes to find what will be REMOVED
- Show those things as DISAPPEARING at tick N (where they still exist and can be selected)
- Filter out REMOVED from current tick's changes (don't show "just disappeared")

**Example:**
- Tick 2: Thing A exists
- Tick 3: Thing A is removed

Old: At tick 3, A shown as REMOVED (can't select - doesn't exist)
New: At tick 2, A shown as DISAPPEARING (can select - exists at tick 2!)
New: At tick 3, A is just gone, not shown

**Edge case:** At the latest tick (live), no DISAPPEARING things shown since we don't know the future.

## Implementation Plan

### 1. Update TypeScript types

**File: `types/index.ts`**

Remove old types and add new `ChangeInfo` interface:

```typescript
// Old (remove):
export type ChangeKind = 'ADDED' | 'MODIFIED' | 'REMOVED';
export interface ThingChange {
  kind: ChangeKind;
  thing: Thing;
}

// New (add):
export interface ChangeInfo {
  addedKeys: Set<string>;
  modifiedKeys: Set<string>;
  disappearingKeys: Set<string>;
}
```

- [x] Remove `ChangeKind` type
- [x] Remove `ThingChange` interface
- [x] Add `ChangeInfo` interface

### 2. Update useTickData hook

**File: `hooks/ticks.ts`**

The hook signature changes to take explicit tick parameters and return three Sets:

```typescript
interface UseTickDataOptions {
  tick: number;           // Required: the tick to show
  prevTick?: number;      // Optional: for ADDED/MODIFIED detection
  nextTick?: number;      // Optional: for disappearing detection
  pause?: boolean;
}

// Bundle the three Sets into a single object for cleaner prop passing
interface ChangeInfo {
  addedKeys: Set<string>;        // keys of things added (vs prevTick)
  modifiedKeys: Set<string>;     // keys of things modified (vs prevTick)
  disappearingKeys: Set<string>; // keys of things that will be gone (at nextTick)
}

interface UseTickDataResult {
  things: Thing[];
  changeInfo: ChangeInfo;        // bundled change Sets
  roots: Root[];
  loading: boolean;
  error: Error | null;
}
```

- [x] Update `useTickData` signature to require `tick` and make `prevTick`/`nextTick` optional
- [x] Change return type: return `changeInfo: ChangeInfo` object containing three Sets
- [x] Add a separate query for next tick's changes (two queries for simplicity)
- [x] Update logic:
  - If `prevTick` provided: query `tick(tick).changes(since: prevTick)` → build `addedKeys` and `modifiedKeys` Sets (ignore REMOVED)
  - If `nextTick` provided: query `tick(nextTick).changes(since: tick)` → build `disappearingKeys` Set from REMOVED items
  - If not provided, return empty Sets
- [x] Memoize the `ChangeInfo` object with `useMemo` to avoid recreating Sets on every render
- [x] Remove `thingsWithRemoved` - no longer needed
- [x] Remove `computeChanges` and `useComputedChanges` (not used)

### 3. Update ThingWatcherProvider

**File: `hooks/ThingWatcherProvider.tsx`**

- [x] Remove `computeChanges` function (unused)
- [x] Update any references to `REMOVED` → `DISAPPEARING` if present

### 4. Update App.tsx (MainContent)

**File: `App.tsx`**

App computes filtered ticks and derives prev/next tick indices from the filtered list (so they match what's shown on the timeline):

```typescript
const { ticks } = useTicks();
const currentTickIndex = resolvedTick;

// Apply level filter, but always include currentTick and latestTick
const filteredTicks = useMemo(() => {
  const latestTickIndex = ticks.length > 0 ? ticks[ticks.length - 1].index : null;

  return ticks.filter(t =>
    // Include if passes level filter (or no filter active)
    (state.levelFilter.length === 0 || state.levelFilter.includes(t.level)) ||
    // Always include current tick
    t.index === currentTickIndex ||
    // Always include latest tick
    t.index === latestTickIndex
  );
}, [ticks, state.levelFilter, currentTickIndex]);

// Find prev/next by searching for closest ticks (handles case where current tick
// might not be exactly at an index in filteredTicks due to timing)
const prevTickIndex = useMemo(() => {
  for (let i = filteredTicks.length - 1; i >= 0; i--) {
    if (filteredTicks[i].index < currentTickIndex) {
      return filteredTicks[i].index;
    }
  }
  return undefined;
}, [filteredTicks, currentTickIndex]);

const nextTickIndex = useMemo(() => {
  if (isLive) return undefined;  // No next tick when live
  for (let i = 0; i < filteredTicks.length; i++) {
    if (filteredTicks[i].index > currentTickIndex) {
      return filteredTicks[i].index;
    }
  }
  return undefined;
}, [filteredTicks, currentTickIndex, isLive]);

const { things, changeInfo, roots } = useTickData({
  tick: currentTickIndex,
  prevTick: prevTickIndex,
  nextTick: nextTickIndex,
});

// Pass to DockviewLayout: things, changeInfo, roots
// Pass both ticks and filteredTicks to Timeline
```

- [x] Move `filteredTicks` computation from Timeline to MainContent
- [x] Always include `currentTickIndex` and `latestTick` in filteredTicks
- [x] Compute `prevTickIndex` as last tick in filteredTicks where `index < currentTickIndex`
- [x] Compute `nextTickIndex` as first tick in filteredTicks where `index > currentTickIndex` (unless live)
- [x] Pass explicit tick params to `useTickData`
- [x] Pass `changeInfo` to DockviewLayout (instead of `changes`)
- [x] Pass both `ticks` and `filteredTicks` to Timeline
- [x] Remove usage of `thingsWithRemoved` (use `things` directly)

### 5. Update Timeline component

**File: `components/Timeline/Timeline.tsx`**

Timeline now receives both raw ticks and filtered ticks as props:
- `ticks`: Raw tick list (for deriving available levels for checkboxes)
- `filteredTicks`: Filtered tick list (for timeline marker display)

- [x] Add `filteredTicks: Tick[]` prop
- [x] Keep `ticks: Tick[]` prop (for level checkbox options)
- [x] Remove internal `filteredTicks` useMemo computation
- [x] Use `filteredTicks` prop for timeline marker positioning and navigation
- [x] Keep level filter UI (checkboxes still update `state.levelFilter` via context)

### 6. Update DockviewLayout

**File: `components/DockviewLayout/DockviewLayout.tsx`**

Update `DockviewLayoutProps` to receive `changeInfo` instead of `changes`:

```typescript
export interface DockviewLayoutProps {
  things: Thing[];
  changeInfo: ChangeInfo;
  selectedThingKey: string | null;
  onSelectThing: (key: string | null) => void;
  // ... other props unchanged
}
```

- [x] Update `DockviewLayoutProps`: replace `changes: ThingChange[]` with `changeInfo: ChangeInfo`
- [x] Update `DockviewPropsContext` to include `changeInfo`
- [x] Pass `changeInfo` to GraphPanelWrapper and ThingsListPanelWrapper

### 7. Update GraphPanelWrapper

**File: `components/DockviewLayout/GraphPanelWrapper.tsx`**

- [x] Pass `changeInfo` from `dockviewProps` to Graph component

### 8. Update Graph component

**File: `components/Graph/Graph.tsx`**

Update props to receive `changeInfo` instead of `changes` array:

```typescript
interface GraphProps {
  things: Thing[];
  changeInfo: ChangeInfo;
  selectedThingKey: string | null;
  onSelectThing: (key: string | null) => void;
}

const getChangeClasses = (key: string): string => {
  const { addedKeys, modifiedKeys, disappearingKeys } = changeInfo;
  const isAdded = addedKeys.has(key);
  const isModified = modifiedKeys.has(key);
  const isDisappearing = disappearingKeys.has(key);

  // Combined states get special classes
  if (isAdded && isDisappearing) return 'addedDisappearing';      // gray
  if (isModified && isDisappearing) return 'modifiedDisappearing'; // purple
  if (isAdded) return 'added';           // green
  if (isModified) return 'modified';     // yellow
  if (isDisappearing) return 'disappearing'; // red
  return '';
};
```

- [x] Update props: replace `changes: ThingChange[]` with `changeInfo: ChangeInfo`
- [x] Remove `changeMap` useMemo (no longer needed - just use Sets directly)
- [x] Update `getChangeClass` → `getChangeClasses` using Set lookups
- [x] Add combined state classes

**File: `components/Graph/Graph.module.css`**

- [x] Rename `.removed` to `.disappearing`
- [x] Change `.disappearing` styles: remove `opacity: 0.5` (full opacity), red dashed border
- [x] Add `.addedDisappearing` class: gray dashed border
- [x] Add `.modifiedDisappearing` class: purple dashed border

### 9. Update ThingsListPanelWrapper

**File: `components/DockviewLayout/ThingsListPanelWrapper.tsx`**

- [x] Pass `changeInfo` from `dockviewProps` to ThingsList component

### 10. Update ThingsList component

**File: `components/ThingsList/ThingsList.tsx`**

Update props to receive `changeInfo`. Show both status badge AND disappearing badge when applicable:

```typescript
interface ThingsListProps {
  things: Thing[];
  changeInfo: ChangeInfo;
  selectedThingKey: string | null;
  onSelectThing: (key: string) => void;
  isLive: boolean;
  triggeredShortIds: Set<number>;
}

// Badges section in list item (show both when applicable):
{changeInfo.addedKeys.has(thing.key) && <span className={styles.addedBadge}>+</span>}
{changeInfo.modifiedKeys.has(thing.key) && <span className={styles.modifiedBadge}>M</span>}
{changeInfo.disappearingKeys.has(thing.key) && <span className={styles.disappearingBadge}>-</span>}

// Sort order helper:
const getSortPriority = (key: string): number => {
  const isAdded = changeInfo.addedKeys.has(key);
  const isModified = changeInfo.modifiedKeys.has(key);
  const isDisappearing = changeInfo.disappearingKeys.has(key);

  if (isAdded && !isDisappearing) return 0;           // added only
  if (isAdded && isDisappearing) return 1;            // added + disappearing
  if (isModified && !isDisappearing) return 2;        // modified only
  if (isModified && isDisappearing) return 3;         // modified + disappearing
  if (isDisappearing) return 4;                       // disappearing only
  return 5;                                           // unchanged
};
```

- [x] Update props: replace `changes: ThingChange[]` with `changeInfo: ChangeInfo`
- [x] Remove `changeMap` useMemo (no longer needed)
- [x] Update badge rendering to use Set lookups (show both badges when applicable)
- [x] Update sort logic with priority: added, added+disappearing, modified, modified+disappearing, disappearing, unchanged
- [x] Update "Changes only" filter: check any of the three Sets
- [x] Update badge titles: `-` now means "About to disappear"

**File: `components/ThingsList/ThingsList.module.css`**

- [x] Rename `.removedBadge` to `.disappearingBadge`
- [x] Rename `.removed` (border class) to `.disappearing`
- [x] Remove opacity reduction if present
- [x] Add `.addedDisappearing` class: gray left border
- [x] Add `.modifiedDisappearing` class: purple left border

### 11. Update ThingPanelContent

**File: `components/ThingPanelContent/ThingPanelContent.tsx`**

- [x] Add `isDisappearing?: boolean` prop
- [x] Add a small red badge in meta header section showing "Disappears next tick"

**File: `components/ThingPanelContent/ThingPanelContent.module.css`**

- [x] Add styles for `.disappearingBadge`

### 12. Update DetailsPanel / DetailsPanelWrapper

**File: `components/DetailsPanel/DetailsPanel.tsx`**

- [x] Add `isDisappearing?: boolean` prop, pass through to ThingPanelContent

**File: `components/DockviewLayout/DetailsPanelWrapper.tsx`**

- [x] Compute `isDisappearing` using the Set from `changeInfo`:
  ```typescript
  const isDisappearing = dockviewProps.selectedThingKey
    ? dockviewProps.changeInfo.disappearingKeys.has(dockviewProps.selectedThingKey)
    : false;
  ```
- [x] Pass `isDisappearing` to DetailsPanel

### 13. Update ThingViewPanel

**File: `components/DockviewLayout/ThingViewPanel.tsx`**

- [x] Compute `isDisappearing` using the Set from `changeInfo`:
  ```typescript
  const isDisappearing = dockviewProps.changeInfo.disappearingKeys.has(key);
  ```
- [x] Pass `isDisappearing` to ThingPanelContent

### 14. Clean up

- [x] Remove `thingsWithRemoved` from `UseTickDataResult` interface
- [x] Remove `ChangeKind` and `ThingChange` types from `types/index.ts`
- [x] Remove `computeChanges` and `useComputedChanges` exports from `hooks/index.ts`
- [x] Search for "REMOVED" to catch any stragglers

### 15. Update LOCAL_CODE_OVERVIEW.md

- [x] Document the three Sets (`addedKeys`, `modifiedKeys`, `disappearingKeys`)
- [x] Update mentions of REMOVED behavior

### 16. Update CODE_OVERVIEW.md

- [x] Update frontend section to mention the three Sets instead of changes array

## Data Flow

```
App.tsx (MainContent):
  1. useTicks() → raw ticks
  2. Apply levelFilter → filteredTicks (always includes currentTick and latestTick)
  3. Derive prevTickIndex = last tick in filteredTicks where index < currentTickIndex
  4. Derive nextTickIndex = first tick in filteredTicks where index > currentTickIndex (unless live)
  5. Call useTickData({ tick, prevTick, nextTick })
  6. Pass things, changeInfo to DockviewLayout
  7. Pass both ticks and filteredTicks to Timeline

useTickData({ tick: N, prevTick, nextTick }):
  Query 1: tick(N).things + tick(N).roots → things and roots
  Query 2 (if prevTick): tick(N).changes(since: prevTick) → build addedKeys, modifiedKeys Sets (ignore REMOVED)
  Query 3 (if nextTick): tick(nextTick).changes(since: N) → build disappearingKeys Set from REMOVED items

  Return: { things, changeInfo: { addedKeys, modifiedKeys, disappearingKeys }, roots, loading, error }

DockviewLayout:
  - Receives things + changeInfo from App
  - Stores in DockviewPropsContext

Graph / ThingsList:
  - Receive changeInfo via props
  - Use Set.has(key) for O(1) lookups

DetailsPanelWrapper / ThingViewPanel:
  - Read changeInfo.disappearingKeys from context
  - isDisappearing = changeInfo.disappearingKeys.has(thingKey)
  - Pass to ThingPanelContent

Timeline:
  - Receives both ticks (for level checkboxes) and filteredTicks (for display)
  - Uses filteredTicks for timeline marker positioning
```

## UI Changes Summary

| Component | Old | New |
|-----------|-----|-----|
| **Data structure** | `changes: ThingChange[]` with `kind` | `changeInfo: ChangeInfo` with three Sets |
| **Lookups** | Search array for matching key | O(1) Set.has(key) |
| **Graph disappearing style** | Red dashed, 50% opacity | Red dashed, full opacity |
| **Graph added+disappearing** | N/A | Gray dashed border |
| **Graph modified+disappearing** | N/A | Purple dashed border |
| **ThingsList badges** | One badge: `+`, `M`, or `-` | Can show both status badge AND `-` for disappearing |
| **ThingsList `-` badge** | "Removed" (thing is gone) | "About to disappear" (thing still exists) |
| **ThingsList sort order** | added, modified, removed | added, added+disappearing, modified, modified+disappearing, disappearing, unchanged |
| **ThingPanelContent** | N/A (couldn't select removed things) | Red badge "Disappears next tick" |

## Testing Considerations

1. **Basic case**: Create thing, tick, remove thing, tick. Verify:
   - At tick before removal: thing shows as DISAPPEARING, can be selected
   - At tick of removal: thing is gone, not shown

2. **Live mode**: At latest tick, no DISAPPEARING shown

3. **Streaming ticks**: Same behavior - if thing disappears in stream tick, show as DISAPPEARING in prior tick

4. **Selection**: Verify DISAPPEARING things can be selected and their data displayed

5. **Graph layout**: Verify DISAPPEARING nodes still participate in layout correctly

6. **Level filtering**: With level filter active (e.g., only "info" ticks), DISAPPEARING should be based on the next *visible* tick, not just N+1. E.g., if viewing tick 5 and ticks 6-9 are filtered out, DISAPPEARING should be based on tick 10's changes.

7. **Combined ADDED + DISAPPEARING**: Create thing at tick N, remove at tick N+1. At tick N, thing should show both `+` badge (ADDED) and `-` badge (DISAPPEARING). Both badges visible in ThingsList, combined styling in Graph.
