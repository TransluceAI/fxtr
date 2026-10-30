# Phase 2 Step 22: Breadcrumbs

## Overview

Add a "breadcrumbs" feature that tracks navigation history between things. When a user navigates from thing A to thing B via a link, a breadcrumb is recorded on B pointing back to A. As long as B is active, A stays active via the breadcrumb. This solves the problem where navigating through a chain of immutable things causes earlier things in the chain to disappear.

**Key properties:**
- Breadcrumbs are per-thing-key (global), not per-panel
- Infinite transitive chaining: C→B→A all stay active as long as C has a root mark
- Multiple breadcrumbs per thing allowed (e.g., reached from different sources)
- Each breadcrumb independently removable by user
- When a thing becomes inactive, all breadcrumbs TO that thing are deleted (cascades transitively)
- Navigation chains (ChildNodeView, hover previews) accumulate pending breadcrumbs that are all created when the user finally navigates

**Breadcrumb types:**
- `link`: Navigated via a forward link (including ChildNodeView content, hover preview content)
- `backlink`: Navigated via a backlink (including backlinks in hover previews)

**Graph styling:**
- "Stable" things (snapshot, pinned, or in a thing-specific panel) display at full opacity
- "Transient" things (only selection or breadcrumb marks) display at reduced opacity
- This indicates which things will disappear if the user navigates away

## Data Model

### Breadcrumb Type

```typescript
interface Breadcrumb {
  /** The thing we navigated FROM */
  fromKey: string;
  /** The thing we navigated TO (the thing this breadcrumb is "attached to") */
  toKey: string;
  /** Type of navigation */
  type: 'link' | 'backlink';
  /** Unique ID for this breadcrumb (for removal) */
  id: string;
}
```

### Breadcrumb Source Context Type

The breadcrumb source context tracks a **chain** of pending breadcrumbs that should be created when the user navigates away. This supports nested structures like ChildNodeView and hover preview cards.

```typescript
interface BreadcrumbSource {
  /** Chain of pending breadcrumbs to create when navigating away */
  pendingBreadcrumbs: Array<Omit<Breadcrumb, 'id'>>;
  /** Current thing key (for adding the next step), or null to explicitly prevent breadcrumb creation */
  currentKey: string | null;
  /** Type of breadcrumb to create for the next navigation */
  type: 'link' | 'backlink';
}
```

**How the chain works:**
- When viewing thing A in a panel: `{ pendingBreadcrumbs: [], currentKey: A, type: 'link' }`
- When thing A contains ChildNodeView for B: `{ pendingBreadcrumbs: [A→B], currentKey: B, type: 'link' }`
- When B contains ChildNodeView for C: `{ pendingBreadcrumbs: [A→B, B→C], currentKey: C, type: 'link' }`
- When user clicks link to D inside C: Creates breadcrumbs A→B, B→C, and C→D all at once

**Context placement:**
- **ThingPanelContent (renderer section)**: Wraps with `type: 'link'` context
- **ThingPanelContent (backlinks section)**: Wraps with `type: 'backlink'` context
- **ChildNodeView**: Extends parent chain with `{ fromKey: parent.currentKey, toKey: childKey, type: 'link' }`
- **HoverPreviewCard**: Extends parent chain with `{ fromKey: parent.currentKey, toKey: hoveredKey, type: parent.type }`
- **BacklinksHoverCard**: Same as HoverPreviewCard but wraps backlinks content with `type: 'backlink'`
- **Graph/ThingsList/Roots**: No provider - direct selection, no breadcrumbs
- **BreadcrumbCard NodeRefs**: Provider with `currentKey: null` - explicitly prevent new breadcrumbs

**Graph hover behavior (important clarification):**
- The Graph itself has no breadcrumb source provider (clicking a graph node = direct selection, no breadcrumb)
- However, HoverPreviewCard extends the parent chain when opened
- If parent has no context (e.g., hovering from Graph), HoverPreviewCard starts a NEW chain with `currentKey` set to the hovered thing but `pendingBreadcrumbs: []`
- This means: hover node A (no breadcrumb yet), hover A's link to B (A→B added to pending), click link to C inside B's preview → creates A→B and B→C breadcrumbs
- The key insight: no "graph→A" breadcrumb because the graph isn't a real source, but the hover chain A→B→C is tracked

## Active Marks System Changes

### New Mark Source: 'breadcrumb'

Add `'breadcrumb'` to the `MarkSource` type:

```typescript
export type MarkSource = 'snapshot' | 'selection' | 'panel' | 'pin' | 'breadcrumb';
```

### Modified Hook: useActiveKeys

Change `useActiveKeys()` to return stability info along with the active keys:

```typescript
// Current:
function useActiveKeys(): Set<string>

// New:
function useActiveKeys(): Map<string, 'stable' | 'transient'>
```

A key is "stable" if it has at least one mark with:
- `source='snapshot'`
- `source='pin'`
- `source='panel'` (thing-specific panels count as stable)

Otherwise it's "transient" (only selection or breadcrumb marks).

```typescript
function useActiveKeys(): Map<string, 'stable' | 'transient'> {
  const marks = useActiveMarks();
  return useMemo(() => {
    const map = new Map<string, 'stable' | 'transient'>();
    for (const mark of marks) {
      const current = map.get(mark.key);
      const isStable = mark.source === 'snapshot' || mark.source === 'pin' || mark.source === 'panel';
      // Upgrade to stable if any mark is stable
      if (!current || (isStable && current === 'transient')) {
        map.set(mark.key, isStable ? 'stable' : 'transient');
      }
    }
    return map;
  }, [marks]);
}
```

### Mount Order Handling

When creating multiple breadcrumbs at once (e.g., A→B, B→C, and panel for C together), there's a timing issue:
1. First render: BreadcrumbMark components check if their toKey is active, but effects haven't run yet
2. Without protection, A→B might see B as inactive and immediately delete itself

The solution is for BreadcrumbMark to track a local "settled" state that becomes true after the first effect cycle. React runs ALL effects from a render batch before triggering re-renders, so by the time `isSettled` becomes true, all registrations from the batch are complete.

This is handled entirely within BreadcrumbMark (no changes to useRegister needed).

## Expanding PinnedThingsContext to Include Breadcrumbs

Rather than creating a separate context, we expand the existing `PinnedThingsContext` to also manage breadcrumbs.

```typescript
interface PinnedThingsContextValue {
  // Existing pin API
  pinnedKeys: Set<string>;
  togglePin(key: string): void;
  pin(key: string): void;
  unpin(key: string): void;
  isPinned(key: string): boolean;

  // New breadcrumb API
  addBreadcrumbs(breadcrumbs: Array<Omit<Breadcrumb, 'id'>>): void;
  removeBreadcrumb(id: string): void;
  breadcrumbsRegistry: Registry;  // For useBreadcrumbsFor hook
}

// Separate hook for efficient breadcrumb queries
function useBreadcrumbsFor(toKey: string): Breadcrumb[];
```

**Note:** `addBreadcrumbs` takes an array because navigation can create multiple breadcrumbs at once (from the pending chain).

### Two Registries

We use two separate Registry instances:

1. **Active marks registry** (existing) - tracks which things are active
2. **Breadcrumbs registry** (new) - allows efficient lookup of breadcrumbs by `toKey`

### Provider Implementation

```typescript
function PinnedThingsProvider({ children }: Props) {
  const activeMarksRegistry = useActiveMarksRegistry();
  const [pinnedKeys, setPinnedKeys] = useState<Set<string>>(new Set());
  const [breadcrumbs, setBreadcrumbs] = useState<Breadcrumb[]>([]);

  // Create breadcrumbs registry once
  const breadcrumbsRegistryRef = useRef<Registry | null>(null);
  if (!breadcrumbsRegistryRef.current) {
    breadcrumbsRegistryRef.current = new Registry();
  }

  // ... pin methods (existing) ...

  // Breadcrumb methods
  const addBreadcrumbs = useCallback((bcs: Array<Omit<Breadcrumb, 'id'>>) => {
    setBreadcrumbs(prev => {
      let next = prev;
      for (const bc of bcs) {
        // Check for existing breadcrumb in same direction
        const hasSameDirection = next.some(
          existing => existing.fromKey === bc.fromKey && existing.toKey === bc.toKey
        );
        // Check for existing breadcrumb in opposite direction
        const hasOppositeDirection = next.some(
          existing => existing.fromKey === bc.toKey && existing.toKey === bc.fromKey
        );

        // Don't create if either exists
        if (hasSameDirection || hasOppositeDirection) {
          continue;
        }

        const id = crypto.randomUUID();
        next = [...next, { ...bc, id }];
      }
      return next;
    });
  }, []);

  const removeBreadcrumb = useCallback((id: string) => {
    setBreadcrumbs(prev => prev.filter(bc => bc.id !== id));
  }, []);

  return (
    <PinnedThingsContext.Provider value={{
      // ... existing pin API ...
      addBreadcrumbs,
      removeBreadcrumb,
      breadcrumbsRegistry: breadcrumbsRegistryRef.current,
    }}>
      {/* Pin marks (existing) */}
      {Array.from(pinnedKeys).map(key => (
        <Registration key={key} registry={activeMarksRegistry} registryKey="activeMarks"
          value={{ key, source: 'pin' }} />
      ))}

      {/* Breadcrumb marks - each handles its own lifecycle */}
      {breadcrumbs.map(bc => (
        <BreadcrumbMark
          key={bc.id}
          breadcrumb={bc}
          onRemove={removeBreadcrumb}
          breadcrumbsRegistry={breadcrumbsRegistryRef.current!}
        />
      ))}

      {children}
    </PinnedThingsContext.Provider>
  );
}
```

### BreadcrumbMark Component

Each `BreadcrumbMark` handles both registration and its own cleanup. It tracks a local "settled" state to avoid premature cleanup during initial mount.

```typescript
function BreadcrumbMark({ breadcrumb, onRemove, breadcrumbsRegistry }: Props) {
  const [isSettled, setIsSettled] = useState(false);
  const activeKeys = useActiveKeys();
  const isToKeyActive = activeKeys.has(breadcrumb.toKey);

  // Register mark for fromKey
  useRegisterActiveMark(breadcrumb.fromKey, 'breadcrumb');

  // Register self in breadcrumbs registry (keyed by toKey) for efficient lookup
  useRegister(breadcrumbsRegistry, breadcrumb.toKey, breadcrumb);

  // Mark as settled after first effect cycle
  // By this point, all registrations from this render batch are complete
  useEffect(() => {
    setIsSettled(true);
  }, []);

  // Self-cleanup: remove breadcrumb when toKey becomes inactive
  // ONLY after settled (ensures all batch registrations are done)
  useEffect(() => {
    if (isSettled && !isToKeyActive) {
      onRemove(breadcrumb.id);
    }
  }, [isSettled, isToKeyActive, breadcrumb.id, onRemove]);

  return null;
}
```

### Cascade Behavior

The cascade happens naturally through React's reactive updates, one layer per render:

1. C becomes inactive (e.g., panel closed)
2. BreadcrumbMark for B→C sees `toKey=C` is inactive (and is settled)
3. BreadcrumbMark calls `onRemove(id)` → breadcrumb removed from state
4. BreadcrumbMark unmounts → B's mark unregistered, breadcrumb unregistered from registry
5. Next render: B is no longer active
6. BreadcrumbMark for A→B sees `toKey=B` is inactive
7. Same cleanup happens
8. Done!

### Cycles

Circular breadcrumbs (e.g., A→B and B→A) would form a stable configuration. To prevent this:

**`addBreadcrumbs` prevents opposite-direction breadcrumbs:**
- No duplicate breadcrumbs (A→B when A→B already exists)
- No opposite breadcrumbs (B→A when A→B already exists)

This means if you navigate A→B→A, only the first breadcrumb (A→B) is created.

## Breadcrumb Source Context

### Context Definition

```typescript
const BreadcrumbSourceContext = createContext<BreadcrumbSource | null>(null);

export function BreadcrumbSourceProvider({ value, children }: {
  value: BreadcrumbSource | null;
  children: React.ReactNode;
}) {
  return (
    <BreadcrumbSourceContext.Provider value={value}>
      {children}
    </BreadcrumbSourceContext.Provider>
  );
}

export function useBreadcrumbSource(): BreadcrumbSource | null {
  return useContext(BreadcrumbSourceContext);
}
```

### ChildNodeView Integration

ChildNodeView extends the parent's breadcrumb chain:

```typescript
function ChildNodeView({ thingKey, keyJSON, args = {} }: ChildNodeViewProps) {
  const parentContext = useBreadcrumbSource();

  // ... existing key resolution logic ...

  const childContext = useMemo((): BreadcrumbSource | null => {
    if (!parentContext?.currentKey || !resolvedThing) return null;

    // Add breadcrumb from parent to this child
    const newBreadcrumb: Omit<Breadcrumb, 'id'> = {
      fromKey: parentContext.currentKey,
      toKey: resolvedThing.key,
      type: 'link', // ChildNodeView always creates link-type breadcrumbs
    };

    return {
      pendingBreadcrumbs: [...parentContext.pendingBreadcrumbs, newBreadcrumb],
      currentKey: resolvedThing.key,
      type: 'link',
    };
  }, [parentContext, resolvedThing?.key]);

  // Wrap content with extended context
  return (
    <BreadcrumbSourceProvider value={childContext}>
      <LowLevelNodeView thingKey={resolvedThing.key} isTopLevel={false} args={args} />
    </BreadcrumbSourceProvider>
  );
}
```

### HoverPreviewCard Integration

HoverPreviewCard extends the parent chain (no special "hover" type).

**Important:** When parent has no context (e.g., hovering from Graph/ThingsList), we start a NEW chain with the hovered thing as `currentKey` but empty `pendingBreadcrumbs`. This allows nested hovers to accumulate breadcrumbs even when the original source has no provider.

```typescript
function HoverPreviewCard({ children, thingKey }: Props) {
  const parentContext = useBreadcrumbSource();

  const hoverContext = useMemo((): BreadcrumbSource => {
    if (!parentContext?.currentKey) {
      // No parent context (e.g., hovering from Graph) - start a new chain
      // This allows nested hovers to still accumulate breadcrumbs
      return {
        pendingBreadcrumbs: [],
        currentKey: thingKey,
        type: 'link',
      };
    }

    // Add breadcrumb from parent to this hovered thing
    const newBreadcrumb: Omit<Breadcrumb, 'id'> = {
      fromKey: parentContext.currentKey,
      toKey: thingKey,
      type: parentContext.type, // Inherit type from parent
    };

    return {
      pendingBreadcrumbs: [...parentContext.pendingBreadcrumbs, newBreadcrumb],
      currentKey: thingKey,
      type: 'link', // Preview content uses link type
    };
  }, [parentContext, thingKey]);

  return (
    <PreviewCard>
      <BreadcrumbSourceProvider value={hoverContext}>
        {/* Preview content - this is the "link" section */}
        <PreviewContent />
      </BreadcrumbSourceProvider>
    </PreviewCard>
  );
}
```

### BacklinksHoverCard Integration

BacklinksHoverCard shows backlinks with the appropriate context. Same pattern as HoverPreviewCard - starts a new chain if parent has no context:

```typescript
function BacklinksHoverCard({ children, thingKey }: Props) {
  const parentContext = useBreadcrumbSource();

  const hoverContext = useMemo((): BreadcrumbSource => {
    if (!parentContext?.currentKey) {
      // No parent context - start a new chain (same as HoverPreviewCard)
      return {
        pendingBreadcrumbs: [],
        currentKey: thingKey,
        type: 'backlink',
      };
    }

    // Add breadcrumb from parent to this hovered thing
    const newBreadcrumb: Omit<Breadcrumb, 'id'> = {
      fromKey: parentContext.currentKey,
      toKey: thingKey,
      type: parentContext.type,
    };

    return {
      pendingBreadcrumbs: [...parentContext.pendingBreadcrumbs, newBreadcrumb],
      currentKey: thingKey,
      type: 'backlink', // Backlinks section uses backlink type
    };
  }, [parentContext, thingKey]);

  // ...similar structure to HoverPreviewCard...
}
```

## NodeRef Changes

When a NodeRef is clicked, it creates all pending breadcrumbs plus the final one:

```typescript
// In useNodeRefData hook
const bcSource = useBreadcrumbSource();
const { addBreadcrumbs } = usePinnedThings();

const handleClick = useCallback((e: React.MouseEvent) => {
  e.preventDefault();
  e.stopPropagation();

  if (thing?.key && bcSource?.currentKey && bcSource.currentKey !== thing.key) {
    // Create the final breadcrumb
    const finalBreadcrumb: Omit<Breadcrumb, 'id'> = {
      fromKey: bcSource.currentKey,
      toKey: thing.key,
      type: bcSource.type,
    };

    // Create all breadcrumbs (pending + final)
    addBreadcrumbs([...bcSource.pendingBreadcrumbs, finalBreadcrumb]);
  }

  onClickThing(thing.key);
}, [bcSource, thing?.key, addBreadcrumbs, onClickThing]);
```

## Drag-to-Panel Breadcrumbs

### Extended ThingDropInfo

```typescript
interface ThingDropInfo {
  key: string;
  keyJSON: unknown;
  shortId: number;
  thingType: string;
  // New: breadcrumbs to create on drop
  pendingBreadcrumbs?: Array<Omit<Breadcrumb, 'id'>>;
}
```

### NodeRef Drag Start

When dragging a NodeRef, include the full pending chain plus the ref itself:

```typescript
const handleDragStart = useCallback((e: React.DragEvent) => {
  if (thing) {
    // Build the breadcrumb chain for this drag
    let pendingBreadcrumbs: Array<Omit<Breadcrumb, 'id'>> = [];
    if (bcSource?.currentKey && bcSource.currentKey !== thing.key) {
      const finalBreadcrumb: Omit<Breadcrumb, 'id'> = {
        fromKey: bcSource.currentKey,
        toKey: thing.key,
        type: bcSource.type,
      };
      pendingBreadcrumbs = [...bcSource.pendingBreadcrumbs, finalBreadcrumb];
    }

    const dropInfo: ThingDropInfo = {
      key: thing.key,
      keyJSON: thing.keyJSON,
      shortId: thing.shortId,
      thingType: thing.thingType,
      pendingBreadcrumbs,
    };
    setThingDragData(e.dataTransfer, dropInfo);
  }
}, [thing, bcSource]);
```

### DraggableIDMarker Drag Start

When dragging a DraggableIDMarker, include the pending chain but don't add a new step (since DraggableIDMarker represents the current thing, not a reference to another thing):

```typescript
// In DraggableIDMarker
const handleDragStart = useCallback((e: React.DragEvent) => {
  if (thingKey) {
    const dropInfo: ThingDropInfo = {
      key: thingKey,
      keyJSON: thingKeyJSON,
      shortId: shortId,
      thingType: thingType,
      // Just the pending breadcrumbs, no new step
      pendingBreadcrumbs: bcSource?.pendingBreadcrumbs ?? [],
    };
    setThingDragData(e.dataTransfer, dropInfo);
  }
}, [thingKey, thingKeyJSON, shortId, thingType, bcSource]);
```

### DockviewLayout Drop Handler

When creating a panel from a drag, create all the pending breadcrumbs:

```typescript
const handleExternalDrop = (dropInfo: ThingDropInfo) => {
  // Create all pending breadcrumbs
  if (dropInfo.pendingBreadcrumbs && dropInfo.pendingBreadcrumbs.length > 0) {
    addBreadcrumbs(dropInfo.pendingBreadcrumbs);
  }
  openThingPanel(dropInfo.key);
};
```

## ThingPanelContent Context Setup

ThingPanelContent wraps its sections with the appropriate breadcrumb contexts:

```typescript
function ThingPanelContent({ thingKey, ... }: Props) {
  // Context for the main content (renderer, forward links)
  const linkContext = useMemo((): BreadcrumbSource => ({
    pendingBreadcrumbs: [],
    currentKey: thingKey,
    type: 'link',
  }), [thingKey]);

  // Context for the backlinks section
  const backlinkContext = useMemo((): BreadcrumbSource => ({
    pendingBreadcrumbs: [],
    currentKey: thingKey,
    type: 'backlink',
  }), [thingKey]);

  return (
    <div className={styles.content}>
      {/* Breadcrumb card (with null context to prevent new breadcrumbs) */}
      <BreadcrumbSourceProvider value={{ pendingBreadcrumbs: [], currentKey: null, type: 'link' }}>
        <BreadcrumbCard thingKey={thingKey} />
      </BreadcrumbSourceProvider>

      {/* Source info, meta header */}
      ...

      {/* Renderer section - link context */}
      <BreadcrumbSourceProvider value={linkContext}>
        <LowLevelNodeView thingKey={thingKey} isTopLevel={true} args={{}} />
      </BreadcrumbSourceProvider>

      {/* Backlinks section - backlink context */}
      <BreadcrumbSourceProvider value={backlinkContext}>
        <BacklinksSection backlinks={thing.backlinks} onSelectThing={onSelectThing} />
      </BreadcrumbSourceProvider>
    </div>
  );
}
```

## Breadcrumb Card UI

Display breadcrumbs in ThingPanelContent, above the source info. Use a collapsible `<details>/<summary>` element so users can collapse it if they don't want to see the breadcrumb list:

```
┌─────────────────────────────────────────┐
│ ▶ Reached from: (2)                     │  ← collapsed summary
├─────────────────────────────────────────┤
│ 📍 Captured in snapshot           [📌]  │
│ ─────────────────────────────────────── │
│ [Thing content...]                      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ ▼ Reached from: (2)                     │  ← expanded
│ ┌─────────────────────────────────┐ [×] │
│ │ [NodeRef to source thing]       │     │
│ │ via link                        │     │
│ └─────────────────────────────────┘     │
│ ┌─────────────────────────────────┐ [×] │
│ │ [NodeRef to other source]       │     │
│ │ via backlink                    │     │
│ └─────────────────────────────────┘     │
├─────────────────────────────────────────┤
│ 📍 Captured in snapshot           [📌]  │
│ ─────────────────────────────────────── │
│ [Thing content...]                      │
└─────────────────────────────────────────┘
```

The BreadcrumbCard is wrapped with a BreadcrumbSourceProvider that sets `currentKey: null` to prevent NodeRefs inside from creating new breadcrumbs (following an existing breadcrumb backwards should not create a new one).

```typescript
function BreadcrumbCard({ thingKey }: { thingKey: string }) {
  const breadcrumbs = useBreadcrumbsFor(thingKey);
  const { removeBreadcrumb } = usePinnedThings();

  if (breadcrumbs.length === 0) {
    return null;
  }

  return (
    <details className={styles.breadcrumbCard} open>
      <summary className={styles.breadcrumbCardHeader}>
        ← Reached from: ({breadcrumbs.length})
      </summary>
      {breadcrumbs.map((bc) => (
        <div key={bc.id} className={styles.breadcrumbEntry}>
          <NodeRef thingKey={bc.fromKey} />
          <span className={styles.breadcrumbType}>via {bc.type}</span>
          <button
            className={styles.breadcrumbRemove}
            onClick={() => removeBreadcrumb(bc.id)}
            title="Remove this breadcrumb"
          >
            ×
          </button>
        </div>
      ))}
    </details>
  );
}
```

## Graph Styling

Add transient vs stable styling to graph nodes:

```typescript
// In ThingNode.tsx
const activeKeys = useActiveKeys();
const stability = activeKeys.get(thing.key);
const isStable = stability === 'stable';

const style = {
  opacity: isStable ? 1 : 0.6,
  // ... existing styles (change state borders, selection)
};
```

Graph styling and change state borders are independent - both can apply simultaneously.

## EmbedApp

The embed view (`/embed`) does not participate in the breadcrumb system. It's a single-thing viewer without navigation context.

## Implementation Steps

### Phase 1: Active Marks Changes

- [ ] 1. Add `'breadcrumb'` to `MarkSource` type
- [ ] 2. Modify `useActiveKeys()` to return `Map<string, 'stable' | 'transient'>` instead of `Set<string>`
   - Stable: snapshot, pin, panel
   - Transient: selection, breadcrumb
- [ ] 3. Update all callers of `useActiveKeys()` to use Map API (`.has()` still works, add `.get()` for stability checks)

### Phase 2: Breadcrumb Source Context

- [ ] 4. Create `BreadcrumbSourceContext` with provider and hook (`useBreadcrumbSource`) in new file `hooks/useBreadcrumbSource.tsx`
- [ ] 5. Refactor ThingPanelContent backlinks to use NodeRef instead of inline click handlers
   - Use flexbox layout: `<NodeRef>` on left, link metadata (linkType, linkKey) on right
   - This centralizes click/breadcrumb logic in NodeRef
   - Backlinks get hover previews and drag-to-panel for free
   - More consistent rendering (uses RefRenderer)
- [ ] 6. Update ThingPanelContent to wrap sections with appropriate breadcrumb contexts:
   - Renderer section: `type: 'link'`
   - Backlinks section: `type: 'backlink'`
- [ ] 7. Update ChildNodeView to extend parent's breadcrumb chain
- [ ] 8. Update HoverPreviewCard to extend parent's breadcrumb chain (with `type: 'link'` for content)
- [ ] 9. Update BacklinksHoverCard to extend parent's breadcrumb chain (with `type: 'backlink'`)

### Phase 3: Expand PinnedThingsContext with Breadcrumbs

- [ ] 10. Add `Breadcrumb` type definition to types
- [ ] 11. Create breadcrumbs registry in `PinnedThingsProvider`
- [ ] 12. Add breadcrumb state and methods (`addBreadcrumbs`, `removeBreadcrumb`) to provider
    - `addBreadcrumbs` takes array, checks for existing same-direction or opposite-direction and skips those
- [ ] 13. Create `BreadcrumbMark` component:
    - Registers mark for `fromKey`
    - Registers self in breadcrumbs registry
    - Tracks local `isSettled` state (true after first effect cycle)
    - Only removes when settled AND toKey is inactive
- [ ] 14. Render `BreadcrumbMark` for each breadcrumb in provider
- [ ] 15. Create `useBreadcrumbsFor(toKey)` hook using breadcrumbs registry
- [ ] 16. Update NodeRef click handler to create breadcrumbs (pending + final)

### Phase 4: Drag-to-Panel Integration

- [ ] 17. Add `pendingBreadcrumbs` field to ThingDropInfo
- [ ] 18. Update NodeRef drag start to include pending chain + final breadcrumb
- [ ] 19. Update DraggableIDMarker drag start to include pending chain (no final breadcrumb)
- [ ] 20. Update DockviewLayout external drop handler to create breadcrumbs
- [ ] 21. Update context menu "Open in new panel" to create breadcrumbs (needs access to breadcrumb context)

### Phase 5: Breadcrumb Card UI

- [ ] 22. Create BreadcrumbCard component showing NodeRefs to source things
- [ ] 23. Integrate BreadcrumbCard into ThingPanelContent (above source info)
- [ ] 24. BreadcrumbCard wraps NodeRefs with null breadcrumb source (currentKey: null)

### Phase 6: Graph Styling

- [ ] 25. Update ThingNode to use `activeKeys.get(key) === 'stable'` for opacity
- [ ] 26. Transient nodes get reduced opacity (0.6), stable nodes get full opacity (1.0)

### Phase 7: Testing & Documentation

- [ ] 27. Test basic navigation: A→B→C, verify all stay active
- [ ] 28. Test breadcrumb removal cascading (close C's panel, verify B and A disappear if no other marks)
- [ ] 29. Test opposite-direction prevention (navigate A→B→A, verify only one breadcrumb created)
- [ ] 30. Test ChildNodeView chains: parent→child→grandchild all get breadcrumbs
- [ ] 31. Test hover preview chains: nested hovers accumulate breadcrumbs
- [ ] 32. Test drag-to-panel with chains: dragging from nested hover creates all breadcrumbs
- [ ] 33. Update CODE_OVERVIEW.md
- [ ] 34. Update LOCAL_CODE_OVERVIEW.md

## Future Considerations

1. **Breadcrumb persistence**: Currently session-only. Could persist to localStorage if useful.
2. **Breadcrumb limits**: Could add a max breadcrumb count per thing or globally.
3. **Visual breadcrumb trail**: Could show breadcrumb chain in a dedicated UI element.
4. **Undo support**: Could support undoing breadcrumb removal.

## Appendix: Original writeup

right now, we have a set of active things which can be marked as active for various reasons, including when a thing is selected. immutable things are only show if they are active. however there's an annoying interaction where, if you select an immutable thing, then select another immutable thing linked to by the first thing, the first one is no longer active so it disappears. ideally it would be nice if you could keep a little breadcrumb trail of how you got to the current thing, even if multiple steps along the way are immutable things with no other reason to be active.

I think this could be solvable as a specific case of a more general feature: local breadcrumbs. The idea of a local breadcrumb is: for each thing key A, we track a list of other thing keys B that the user was viewing previously that they then used to navigate from B to A, along with a small metadata object of how they navigated. Then, as long as A is active, and as long as the breadcrumb exists, B should also be kept active. To give users control of this, we can show these breadcrumbs for thing A in a small card above each instance of thing A's thing panel contents, and allow them to click an "x" button to remove the breadcrumb (which would then unmark B). Additionally, if A becomes inactive, we would remove all breadcrumbs for A (which might then transitively propagate up and deactivate things like B unless they had other reasons to be active).

I'm thinking the breadcrumbs system could be attached to the PinnedThingsContext, since the breadcrumbs are related to pinning and should be kept in sync.

Additionally as part of this change I want to modify the graph styling a bit, so that graph nodes that are active only because of (a) being open in a panel, (b) being selected, or (c) being a breadcrumb, are styled differently, perhaps reduced opacity. Things that are either (d) in the current snapshot or (e) pinned should be fully opaque. This then tells the user which things are in danger of disappearing if the user navigates away from them.

The breadcrumb tracking should occur for ANY way of going from one thing to another in the UI, including following refs or dragging things into panels. The source of the breadcrumb could be set in a context and then either passed through in drag data or inspected at the point of handling a click event. Hover preview cards need a bit of special treatment because they support nested hover preview cards; in this case we should still attribute them to the _original source_ of the preview card, but give a special breadcrumb type indicating that it was through a hover preview. Directly selecting things in the graph, things list, or roots should not add any breadcrumbs; this also applies to hover preview cards that were originally sourced from the graph, things list, or roots.

The breadcrumbs view card should contain NodeRefs that link back to the previous step in the breadcrumb trail. Clicking _these_ NodeRefs should NOT add a new breadcrumb, since we are just following an existing breadcrumb backwards.

We will want breadcrumb types for "link", "backlink", and "hover card".

### Additional design notes (supersede parts of original writeup)

1. ChildNodeView chains: It is possible for a thing to display another thing inline via a ChildNodeView. We want breadcrumbs to reflect both the link coming from the child AND the child coming from the parent. The breadcrumb context holds a list of pending breadcrumbs that are all created when the user finally navigates. ChildNodeView acts like a forward link.

2. Hover cards simplified: Instead of a special "hover" type, hover cards just extend the chain like ChildNodeView. Preview content uses "link" type, backlinks inside hovers use "backlink" type. No special hover breadcrumb type needed.

3. Drag with chains: Dragging a NodeRef carries the pending chain plus adds itself. Dragging a DraggableIDMarker carries the pending chain without adding anything (it represents the current thing, not a reference).

4. Mount order safety: BreadcrumbMark tracks a local `isSettled` state that becomes true after the first effect cycle. Cleanup only runs when settled, ensuring all marks from a batch register before any cleanup runs. No changes to useRegister needed.

5. Panel stability: Thing-specific panels (not the details panel) count as "stable" marks, so things with open panels appear at full opacity.
