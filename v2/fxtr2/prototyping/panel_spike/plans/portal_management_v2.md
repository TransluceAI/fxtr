# Portal Management v2

This document describes how views and portals are managed in the panel spike.

## Overview

The system has three main parts:

1. **ViewRegistry** - A plain object (not a React component) that manages all state: views, slots, panels, and the dockview API. It uses the external store pattern for React integration.

2. **RenderRoot** - A React component that subscribes to the registry and renders:
   - InPortals for all active views (in a hidden container)
   - DockviewReact (which contains the panels with OutPortals)

3. **Child** - A React component used by entity renderers to display owned children. It registers a "slot" with the registry and renders either an OutPortal (if it owns a view) or a placeholder.

```
┌─────────────────────────────────────────────────────────────────┐
│ ViewRegistry (external store)                                   │
│   - views: Map<ViewId, View>                                    │
│   - slots: Map<SlotId, Slot>                                    │
│   - panels: Map<PanelId, Panel>                                 │
│   - dockviewApi: DockviewApi                                    │
└─────────────────────────────────────────────────────────────────┘
         │
         │ subscribe / getSnapshot
         ▼
┌─────────────────────────────────────────────────────────────────┐
│ RenderRoot (React component)                                    │
│  ┌────────────────────┐  ┌───────────────────────────────────┐  │
│  │ InPortal Container │  │ DockviewReact                     │  │
│  │ (hidden)           │  │  ┌─────────────────────────────┐  │  │
│  │                    │  │  │ EntityPanel                 │  │  │
│  │ <InPortal>         │  │  │   <OutPortal node={...} />  │  │  │
│  │   <EntityRenderer/>│  │  └─────────────────────────────┘  │  │
│  │ </InPortal>        │  │  ┌─────────────────────────────┐  │  │
│  │ ...                │  │  │ DebugPanel                  │  │  │
│  └────────────────────┘  │  └─────────────────────────────┘  │  │
│                          └───────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Types

```typescript
type ViewId = string;
type SlotId = string;
type PanelId = string;
type EntityId = string;

interface View {
  viewId: ViewId;
  entityId: EntityId;
  portalNode: HtmlPortalNode;
  ownerSlotId: SlotId | null;  // non-null if owned by inline slot
  panelId: PanelId | null;      // non-null if owned by panel
}

interface Slot {
  slotId: SlotId;
  entityId: EntityId;
  parentViewId: ViewId;  // the view that contains this slot
}

interface Panel {
  panelId: PanelId;
  viewId: ViewId;
}

interface ViewRegistryState {
  views: Map<ViewId, View>;
  slots: Map<SlotId, Slot>;
  panels: Map<PanelId, Panel>;
}

// What getSlotContent returns
type SlotContent =
  | { type: 'view'; portalNode: HtmlPortalNode }
  | { type: 'placeholder'; viewId: ViewId }  // viewId of existing view
  | { type: 'pending' };
```

## ViewRegistry API

```typescript
interface ViewRegistry {
  // --- State access (for useSyncExternalStore) ---
  subscribe(listener: () => void): () => void;
  getSnapshot(): ViewRegistryState;

  // --- Slot operations (called by Child component) ---
  // Combined subscribe + register: registers slot, subscribes to updates,
  // and unregisters on cleanup. Use with useSyncExternalStore.
  subscribeSlot(
    slotId: SlotId,
    entityId: EntityId,
    parentViewId: ViewId,
    listener: () => void
  ): () => void;
  getSlotContent(slotId: SlotId): SlotContent;

  // --- View access (called by EntityPanel) ---
  getView(viewId: ViewId): View | undefined;

  // --- Entity operations (called by click handlers) ---
  openEntity(entityId: EntityId): void;

  // --- Dockview integration ---
  setDockviewApi(api: DockviewApi): void;
  handlePanelClose(panelId: PanelId): void;
}
```

## ViewRegistry Implementation

### State Management

The registry uses the external store pattern:

```typescript
function createViewRegistry(): ViewRegistry {
  let state: ViewRegistryState = {
    views: new Map(),
    slots: new Map(),
    panels: new Map(),
  };
  let dockviewApi: DockviewApi | null = null;
  const listeners = new Set<() => void>();

  function notify() {
    listeners.forEach(l => l());
  }

  function subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function getSnapshot() {
    return state;
  }

  // ... methods below
}
```

### subscribeSlot

Combined registration and subscription. Called via useSyncExternalStore in Child.
This registers the slot on subscribe, and unregisters on cleanup.

```typescript
function subscribeSlot(
  slotId: SlotId,
  entityId: EntityId,
  parentViewId: ViewId,
  listener: () => void
): () => void {
  // Register the slot
  registerSlotInternal(slotId, entityId, parentViewId);

  // Subscribe to updates
  listeners.add(listener);

  // Return cleanup function
  return () => {
    listeners.delete(listener);
    unregisterSlotInternal(slotId);
  };
}
```

### registerSlotInternal (private)

```typescript
function registerSlotInternal(slotId: SlotId, entityId: EntityId, parentViewId: ViewId) {
  // Create the slot
  const slot: Slot = { slotId, entityId, parentViewId };

  // Check if this is the first slot for this entity
  const existingViews = getViewsForEntity(entityId);

  if (existingViews.length === 0) {
    // First slot: create a view and bind it to this slot
    const viewId = generateViewId();
    const portalNode = createHtmlPortalNode();
    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: slotId,
      panelId: null,
    };
    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
      slots: new Map(state.slots).set(slotId, slot),
    };
  } else {
    // Not first: this slot is a placeholder
    state = {
      ...state,
      slots: new Map(state.slots).set(slotId, slot),
    };
  }

  notify();
}
```

### unregisterSlotInternal (private)

```typescript
function unregisterSlotInternal(slotId: SlotId) {
  const slot = state.slots.get(slotId);
  if (!slot) return;

  // Find if this slot owns a view
  const ownedView = Array.from(state.views.values())
    .find(v => v.ownerSlotId === slotId);

  // Remove the slot
  const newSlots = new Map(state.slots);
  newSlots.delete(slotId);

  if (ownedView) {
    // This slot owned a view - need to rebind or destroy
    const otherSlots = Array.from(newSlots.values())
      .filter(s => s.entityId === slot.entityId);

    if (otherSlots.length > 0) {
      // Rebind view to another slot
      const newOwner = otherSlots[0];
      const updatedView: View = {
        ...ownedView,
        ownerSlotId: newOwner.slotId,
      };
      state = {
        ...state,
        slots: newSlots,
        views: new Map(state.views).set(ownedView.viewId, updatedView),
      };
    } else {
      // No other slots - destroy view
      const newViews = new Map(state.views);
      newViews.delete(ownedView.viewId);
      state = {
        ...state,
        slots: newSlots,
        views: newViews,
      };
    }
  } else {
    state = { ...state, slots: newSlots };
  }

  notify();
}
```

### getSlotContent

Called by Child component to determine what to render.

```typescript
function getSlotContent(slotId: SlotId): SlotContent {
  const slot = state.slots.get(slotId);

  if (!slot) {
    // Slot not registered yet (first render before useLayoutEffect)
    return { type: 'pending' };
  }

  // Check if this slot owns a view
  const ownedView = Array.from(state.views.values())
    .find(v => v.ownerSlotId === slotId);

  if (ownedView) {
    return { type: 'view', portalNode: ownedView.portalNode };
  }

  // This slot is a placeholder - find the view that exists
  const existingView = Array.from(state.views.values())
    .find(v => v.entityId === slot.entityId);

  if (existingView) {
    return { type: 'placeholder', viewId: existingView.viewId };
  }

  // Shouldn't happen, but handle gracefully
  return { type: 'pending' };
}
```

### openEntity

Called when user clicks a ref to open an entity.

```typescript
function openEntity(entityId: EntityId) {
  const existingViews = getViewsForEntity(entityId);

  if (existingViews.length > 0) {
    // View exists - focus it
    // TODO (phase 2): scroll to view, highlight it
    console.log(`TODO: focus existing view for entity ${entityId}`);
    return;
  }

  // No view exists - create one in a new panel
  const viewId = generateViewId();
  const panelId = `panel-${viewId}`;
  const portalNode = createHtmlPortalNode();

  const view: View = {
    viewId,
    entityId,
    portalNode,
    ownerSlotId: null,
    panelId,
  };

  const panel: Panel = {
    panelId,
    viewId,
  };

  state = {
    ...state,
    views: new Map(state.views).set(viewId, view),
    panels: new Map(state.panels).set(panelId, panel),
  };

  notify();

  // Open the panel in dockview
  if (dockviewApi) {
    dockviewApi.addPanel({
      id: panelId,
      component: 'entity',
      title: entityId,  // TODO: better title from entity data
      params: { viewId },
    });
  }
}
```

### handlePanelClose

Called when a dockview panel is closed.

```typescript
function handlePanelClose(panelId: PanelId) {
  const panel = state.panels.get(panelId);
  if (!panel) return;

  const view = state.views.get(panel.viewId);
  if (!view) return;

  // Remove the panel
  const newPanels = new Map(state.panels);
  newPanels.delete(panelId);

  // Check if there are slots that could adopt this view
  const eligibleSlots = Array.from(state.slots.values())
    .filter(s => s.entityId === view.entityId);

  if (eligibleSlots.length > 0) {
    // Rebind view to a slot
    const newOwner = eligibleSlots[0];
    const updatedView: View = {
      ...view,
      ownerSlotId: newOwner.slotId,
      panelId: null,
    };
    state = {
      ...state,
      panels: newPanels,
      views: new Map(state.views).set(view.viewId, updatedView),
    };
  } else {
    // No eligible slots - destroy view
    const newViews = new Map(state.views);
    newViews.delete(view.viewId);
    state = {
      ...state,
      panels: newPanels,
      views: newViews,
    };
  }

  notify();
}
```

### Helper functions

```typescript
function getViewsForEntity(entityId: EntityId): View[] {
  return Array.from(state.views.values())
    .filter(v => v.entityId === entityId);
}

function generateViewId(): ViewId {
  return `view-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function setDockviewApi(api: DockviewApi) {
  dockviewApi = api;
}

function getView(viewId: ViewId): View | undefined {
  return state.views.get(viewId);
}
```

## React Components

### ViewRegistryContext

The registry is provided via React context so Child components and click handlers can access it.

```typescript
const ViewRegistryContext = createContext<ViewRegistry | null>(null);

function useViewRegistry(): ViewRegistry {
  const registry = useContext(ViewRegistryContext);
  if (!registry) throw new Error('useViewRegistry must be used within ViewRegistryProvider');
  return registry;
}
```

### RenderRoot

Subscribes to the registry and renders InPortals + Dockview.

```typescript
interface RenderRootProps {
  data: DataBundle;
  viewRegistry: ViewRegistry;
}

function RenderRoot({ data, viewRegistry }: RenderRootProps) {
  // Subscribe to viewRegistry state for views
  const views = useSyncExternalStore(
    viewRegistry.subscribe,
    () => Array.from(viewRegistry.getSnapshot().views.values())
  );

  // Handle dockview ready
  const onDockviewReady = useCallback((event: DockviewReadyEvent) => {
    viewRegistry.setDockviewApi(event.api);

    // Set up panel close handler
    event.api.onDidRemovePanel((panel) => {
      viewRegistry.handlePanelClose(panel.id);
    });

    // Create initial panels (debug, entrypoint)
    event.api.addPanel({
      id: 'debug',
      component: 'debug',
      title: 'Debug',
      params: { data, viewRegistry },
    });
    event.api.addPanel({
      id: 'entrypoint',
      component: 'entrypoint',
      title: 'Entrypoint',
      params: { data, viewRegistry },
      position: { referencePanel: 'debug', direction: 'right' },
    });
  }, [viewRegistry, data]);

  return (
    <>
      {/* Hidden InPortal container */}
      <div style={{ display: 'none' }}>
        {views.map(view => (
          <InPortal key={view.viewId} node={view.portalNode}>
            <CurrentViewContext.Provider value={view.viewId}>
              <EntityRenderer
                entityId={view.entityId}
                data={data}
                viewRegistry={viewRegistry}
              />
            </CurrentViewContext.Provider>
          </InPortal>
        ))}
      </div>

      {/* Dockview */}
      <DockviewReact
        className="dockview-theme-abyss"
        onReady={onDockviewReady}
        components={{
          debug: DebugPanel,
          entrypoint: EntrypointPanel,
          entity: EntityPanel,
        }}
      />
    </>
  );
}
```

### CurrentViewContext

Provides the current view ID to child components (so Child knows its parentViewId).

```typescript
const CurrentViewContext = createContext<ViewId | null>(null);

function useCurrentViewId(): ViewId {
  const viewId = useContext(CurrentViewContext);
  if (!viewId) throw new Error('useCurrentViewId must be used within a view');
  return viewId;
}
```

### Child Component

Used by entity renderers to display owned children.

The key insight: we combine slot registration with the useSyncExternalStore subscription.
The `subscribeSlot` function registers the slot when subscribing, and unregisters on cleanup.
This eliminates the need for a separate useLayoutEffect.

```typescript
interface ChildProps {
  entityId: EntityId;
  // customArgs can be added later
}

function Child({ entityId }: ChildProps) {
  const viewRegistry = useViewRegistry();
  const slotId = useId();
  const parentViewId = useCurrentViewId();

  // Memoize the subscribe function - it captures slotId, entityId, parentViewId
  const subscribeToSlot = useCallback(
    (listener: () => void) =>
      viewRegistry.subscribeSlot(slotId, entityId, parentViewId, listener),
    [viewRegistry, slotId, entityId, parentViewId]
  );

  // Single useSyncExternalStore handles both registration and subscription
  const content = useSyncExternalStore(
    subscribeToSlot,
    () => viewRegistry.getSlotContent(slotId)
  );

  if (content.type === 'pending') {
    return (
      <div style={{ color: '#888', fontStyle: 'italic' }}>
        [Pending slot {slotId}]
      </div>
    );
  }

  if (content.type === 'placeholder') {
    return (
      <Placeholder
        entityId={entityId}
        viewId={content.viewId}
        viewRegistry={viewRegistry}
      />
    );
  }

  // content.type === 'view'
  return <OutPortal node={content.portalNode} />;
}
```

### Placeholder Component

Shown when entity is already rendered elsewhere.

```typescript
interface PlaceholderProps {
  entityId: EntityId;
  viewId: ViewId;
  viewRegistry: ViewRegistry;
}

function Placeholder({ entityId, viewId, viewRegistry }: PlaceholderProps) {
  const handleClick = () => {
    // TODO (phase 2): scroll to the view
    console.log(`TODO: jump to view ${viewId} for entity ${entityId}`);
  };

  return (
    <div
      onClick={handleClick}
      style={{
        padding: '4px 8px',
        background: '#3a3a3a',
        border: '1px dashed #666',
        borderRadius: 4,
        cursor: 'pointer',
        color: '#888',
      }}
    >
      [{entityId.slice(0, 8)}...] open elsewhere (TODO)
    </div>
  );
}
```

### EntityRenderer (Phase 1: Hardcoded)

Renders the content of an entity. For phase 1, this is a simple hardcoded if-else
that renders each entity type with a colored outline. Uses `<Child>` to render
owned children.

**Phase 2**: Replace with dynamic renderer registry per the spec's "type renderer registry"
(Full, Preview, Ref renderers per type).

```typescript
interface EntityRendererProps {
  entityId: EntityId;
  data: DataBundle;
  viewRegistry: ViewRegistry;
}

function EntityRenderer({ entityId, data, viewRegistry }: EntityRendererProps) {
  const entity = getData(data, entityId);

  if (!entity) {
    return <div style={{ color: '#f88' }}>Entity not found: {entityId}</div>;
  }

  const borderColor = getTypeColor(entity.type);

  return (
    <div
      style={{
        border: `2px solid ${borderColor}`,
        borderRadius: 4,
        padding: 12,
        background: '#2a2a2a',
      }}
    >
      <div style={{ marginBottom: 8, color: borderColor, fontWeight: 'bold' }}>
        {entity.type}: {entityId}
      </div>

      {/* Render type-specific content */}
      {entity.type === 'collection' && (
        <CollectionContent entity={entity} />
      )}
      {entity.type === 'transcript' && (
        <TranscriptContent entity={entity} />
      )}
      {entity.type === 'turn' && (
        <TurnContent entity={entity} data={data} />
      )}
      {entity.type === 'judgement_set' && (
        <JudgementSetContent entity={entity} />
      )}
      {entity.type === 'judgement' && (
        <JudgementContent entity={entity} data={data} />
      )}
      {entity.type === 'rubric' && (
        <RubricContent entity={entity} />
      )}
      {entity.type === 'misconfigured' && (
        <MisconfiguredContent entity={entity} />
      )}
    </div>
  );
}

// Example content component - renders owned children using <Child>
function TranscriptContent({ entity }: { entity: Transcript }) {
  return (
    <div>
      <div style={{ color: '#888', marginBottom: 8 }}>Topic: {entity.topic}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {entity.turns.map(turnId => (
          <Child key={turnId} entityId={turnId} />
        ))}
      </div>
    </div>
  );
}

// Similar content components for other types...
// Each uses <Child> for owned children and shows refs as text (for now)
```

### EntityPanel

Panel that displays an entity at the top level.

```typescript
interface EntityPanelParams {
  viewId: ViewId;
}

function EntityPanel({ params }: IDockviewPanelProps<EntityPanelParams>) {
  const { viewId } = params;
  const viewRegistry = useViewRegistry();

  // Subscribe to view (in case it changes, though unlikely)
  const view = useSyncExternalStore(
    viewRegistry.subscribe,
    () => viewRegistry.getView(viewId)
  );

  if (!view) {
    return <div>View not found</div>;
  }

  return (
    <div style={{ padding: 16, height: '100%', overflow: 'auto' }}>
      <OutPortal node={view.portalNode} />
    </div>
  );
}
```

### EntrypointPanel (updated)

Now uses registry to open entities.

```typescript
function EntrypointPanel({ params }: IDockviewPanelProps<EntrypointPanelParams>) {
  const { data, viewRegistry } = params;
  const topLevel = getTopLevelEntities(data);

  return (
    <div style={{ padding: 16, height: '100%', overflow: 'auto' }}>
      <h3 style={{ marginTop: 0 }}>Entrypoint</h3>
      <ul style={{ listStyle: 'none', padding: 0 }}>
        {topLevel.map(id => {
          const entity = getData(data, id);
          return (
            <li
              key={id}
              onClick={() => viewRegistry.openEntity(id)}
              style={{
                padding: '4px 8px',
                marginBottom: 4,
                background: '#3d3d3d',
                borderRadius: 4,
                cursor: 'pointer',
              }}
            >
              <code style={{ color: '#58a6ff' }}>{id}</code>
              <span style={{ color: '#888', marginLeft: 8 }}>
                ({entity?.type})
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
```

## App Setup

```typescript
function App() {
  // Create registry once
  const viewRegistry = useMemo(() => createViewRegistry(), []);

  // Create data bundle once
  const data = useMemo(() => createDataBundle(exampleEntities), []);

  return (
    <ViewRegistryContext.Provider value={viewRegistry}>
      <div style={{ width: '100vw', height: '100vh' }}>
        <RenderRoot data={data} viewRegistry={viewRegistry} />
      </div>
    </ViewRegistryContext.Provider>
  );
}
```

## Interaction Flows

### Opening an Entity (click ref in Entrypoint)

```
1. User clicks entity in EntrypointPanel
2. EntrypointPanel calls registry.openEntity(entityId)
3. Registry checks: any views for this entity?
   - YES: log "TODO focus" (phase 2)
   - NO: continue...
4. Registry creates View (viewId, portalNode, panelId)
5. Registry creates Panel entry
6. Registry calls notify() → RenderRoot re-renders
7. RenderRoot renders new InPortal for the view
8. Registry calls dockviewApi.addPanel({ params: { viewId } })
9. Dockview creates panel, renders EntityPanel
10. EntityPanel renders OutPortal → content appears
```

### Child Renders Entity (first time)

```
1. Parent renderer uses <Child entityId={X} />
2. Child renders, useSyncExternalStore calls subscribeSlot(...)
3. subscribeSlot: registers slot, no views for X → creates View bound to slot
4. subscribeSlot: calls notify(), adds listener, returns cleanup
5. getSlotContent(slotId) returns { type: 'view', portalNode }
6. Child renders OutPortal → content appears
```

### Child Renders Entity (already exists elsewhere)

```
1. Parent renderer uses <Child entityId={X} />
2. Child renders, useSyncExternalStore calls subscribeSlot(...)
3. subscribeSlot: registers slot, view already exists → slot is placeholder
4. subscribeSlot: calls notify(), adds listener, returns cleanup
5. getSlotContent(slotId) returns { type: 'placeholder', viewId }
6. Child renders Placeholder component
```

### Panel Closes (view has placeholder elsewhere)

```
1. User closes entity panel
2. Dockview emits onDidRemovePanel
3. RenderRoot calls registry.handlePanelClose(panelId)
4. Registry finds view for this panel
5. Registry finds slots for same entity → rebinds view to first slot
6. Registry removes panel entry
7. Registry calls notify()
8. The slot that was a placeholder now owns the view
9. Slot re-renders with OutPortal instead of Placeholder
```

### Panel Closes (no placeholders)

```
1. User closes entity panel
2. Dockview emits onDidRemovePanel
3. RenderRoot calls registry.handlePanelClose(panelId)
4. Registry finds view for this panel
5. Registry finds no slots for same entity
6. Registry destroys view, removes panel entry
7. Registry calls notify()
8. RenderRoot removes the InPortal
```

## Phase 2 TODOs

These are deferred for later implementation:

### Navigation
- [ ] Jumping to a view when clicking ref (scroll into view, highlight)
- [ ] Jumping to a view when clicking placeholder
- [ ] Focus/highlight animation when jumping to a view

### View Management
- [ ] "Move entity here" action on placeholder (right-click menu)
- [ ] "Open copy here" action (creates second view for same entity)

### Renderer Registry
- [ ] Dynamic renderer registry per spec's "type renderer registry"
- [ ] Full renderer (card view, uses Child for owned, Ref for references)
- [ ] Preview renderer (smaller card for hover previews)
- [ ] Ref renderer (one-line inline reference)
- [ ] Replace hardcoded EntityRenderer with registry lookup

### UI Polish
- [ ] Better panel titles (from entity data, not just ID)
- [ ] Parent/backref display above/below entity content in panels
- [ ] Hover previews (PreviewCard from base-ui)

## Fix: Snapshot Caching for useSyncExternalStore

### Problem
`useSyncExternalStore` requires `getSnapshot` to return a **stable/cached** value. Creating new arrays/objects on each call causes infinite re-render loops.

### Solution: Registry-owned caching with custom hooks

ViewRegistry exposes stable `getSnapshot` functions that return cached values. Caches are invalidated (set to null) on state change and repopulated synchronously on next read.

**Cached state in ViewRegistry:**
```typescript
let viewsArrayCache: View[] | null = null;
let slotContentCache = new Map<SlotId, SlotContent>();
```

**Granular listeners:**
```typescript
const viewListeners = new Set<() => void>();  // for useViews
const slotListeners = new Map<SlotId, () => void>();  // one per slot
```

**Granular notification:**
```typescript
function notifyViews() {
  viewsArrayCache = null;
  viewListeners.forEach(l => l());
}

function notifySlot(slotId: SlotId) {
  slotContentCache.delete(slotId);
  slotListeners.get(slotId)?.();
}

function notifySlotsForEntity(entityId: EntityId) {
  // Find all slots for this entity and notify them
  for (const [slotId, slot] of state.slots) {
    if (slot.entityId === entityId) {
      notifySlot(slotId);
    }
  }
}
```

**When to notify what:**
- `registerSlotInternal`: notifyViews (if view created), notifySlot (this slot)
- `unregisterSlotInternal`: notifyViews (if view destroyed/rebound), notifySlotsForEntity (if view rebound)
- `openEntity`: notifyViews, notifySlotsForEntity (existing placeholders become aware)
- `handlePanelClose`: notifyViews, notifySlotsForEntity (if view rebound to slot)

**Stable getSnapshot functions:**
```typescript
function getViewsSnapshot(): View[] {
  if (viewsArrayCache === null) {
    viewsArrayCache = Array.from(state.views.values());
  }
  return viewsArrayCache;
}

function getSlotContentSnapshot(slotId: SlotId): SlotContent {
  if (!slotContentCache.has(slotId)) {
    slotContentCache.set(slotId, computeSlotContent(slotId));
  }
  return slotContentCache.get(slotId)!;
}
```

**Custom hooks (in separate file or exported from registry):**
```typescript
export function useViews(registry: ViewRegistry): View[] {
  return useSyncExternalStore(
    registry.subscribeViews,
    registry.getViewsSnapshot
  );
}

export function useSlotContent(
  registry: ViewRegistry,
  slotId: SlotId,
  entityId: EntityId,
  parentViewId: ViewId
): SlotContent {
  const subscribeToSlot = useCallback(
    (listener: () => void) =>
      registry.subscribeSlot(slotId, entityId, parentViewId, listener),
    [registry, slotId, entityId, parentViewId]
  );

  return useSyncExternalStore(
    subscribeToSlot,
    () => registry.getSlotContentSnapshot(slotId)
  );
}
```

**Key insight:** `useSyncExternalStore` compares the *return value* of getSnapshot using `Object.is`, not the function reference. So the closure `() => registry.getSlotContentSnapshot(slotId)` is fine as long as it returns the same cached object.

### Changes needed
1. Add cache fields (`viewsArrayCache`, `slotContentCache`) to ViewRegistry
2. Add granular listeners (`viewListeners`, `slotListeners`) to ViewRegistry
3. Add `subscribeViews()`, `getViewsSnapshot()`, `getSlotContentSnapshot()` to ViewRegistry
4. Replace `notify()` with granular `notifyViews()`, `notifySlot()`, `notifySlotsForEntity()`
5. Update `subscribeSlot` to use `slotListeners` instead of generic `listeners`
6. Create `useViews` and `useSlotContent` hooks (in contexts.tsx or new hooks.ts)
7. Update RenderRoot to use `useViews(viewRegistry)`
8. Update Child to use `useSlotContent(viewRegistry, ...)`


## Phase 2a

Goals of phase 2a:

Navigation
- Jumping to a view when clicking ref or placeholder
- Focus/highlight animation when jumping to a view
  - Focus the panel it is in, if necessary
  - Scroll it to view
  - Activate its highlight briefly

View Management right-click menus:
- On a placeholder:
  - "Move entity view here" action (only enabled if there is exactly one place this entity is open; disabled with a message if there is ambiguity)
  - "Open new entity view here" action (creates second view for same entity)
- On a child:
  - "Move entity view to new tab" action
  - "Open another view in new tab" action
- On a root entity:
  - "Open another view in new tab" action
- On a ref:
  - "Open a view in new tab" action
  - "Jump to existing view" action (disabled if there isn't one)

For right click menu, use https://base-ui.com/react/components/context-menu. But have shift right-click open the builtin menu. Also have a disabled line at the bottom of the custom context menu saying "(hold shift for browser's context menu)"

For highlighting, the panel root or child elements should use a wrapper element that gets styled via CSS. Highlight state can be managed by the view registry.

## Phase 2a Implementation Notes

### Navigation: Jumping to a View

**Finding a view's panel:**
```typescript
function findViewPanel(viewId: ViewId): PanelId | null {
  const view = state.views.get(viewId);
  if (!view) return null;
  if (view.panelId) return view.panelId;
  if (view.ownerSlotId) {
    const slot = state.slots.get(view.ownerSlotId);
    if (slot) return findViewPanel(slot.parentViewId);  // Recurse up
  }
  return null;
}
```

**jumpToView(viewId) implementation:**
1. Find panel via `findViewPanel(viewId)`
2. Focus the panel: `dockviewApi.getPanel(panelId)?.focus()`
3. Scroll to element: `document.querySelector([data-view-id="${viewId}"])?.scrollIntoView({ behavior: 'smooth', block: 'center' })`
4. Highlight the view (see below)

**Choosing which view to jump to (for refs with multiple views):**
- Prefer a view in a currently visible/active panel
- Otherwise pick arbitrarily (first in Map iteration)

### Highlighting

**State in ViewRegistry:**
```typescript
let highlightedViewId: ViewId | null = null;
let highlightTimeoutId: ReturnType<typeof setTimeout> | null = null;
const highlightListeners = new Set<() => void>();

function highlightView(viewId: ViewId, durationMs = 1500) {
  if (highlightTimeoutId) clearTimeout(highlightTimeoutId);
  highlightedViewId = viewId;
  notifyHighlight();
  highlightTimeoutId = setTimeout(() => {
    highlightedViewId = null;
    notifyHighlight();
  }, durationMs);
}

function getHighlightedViewId(): ViewId | null { return highlightedViewId; }
function subscribeHighlight(l: () => void) { highlightListeners.add(l); return () => highlightListeners.delete(l); }
function notifyHighlight() { highlightListeners.forEach(l => l()); }
```

**CSS styling:** White border glow via `box-shadow: 0 0 8px 2px white` on wrapper with `data-view-id`.

**useHighlight hook:**
```typescript
function useIsHighlighted(viewId: ViewId): boolean {
  return useSyncExternalStore(
    registry.subscribeHighlight,
    () => registry.getHighlightedViewId() === viewId
  );
}
```

### DOM Element Identification

Add `data-view-id={viewId}` attribute to EntityRenderer's wrapper div. This enables:
- `scrollIntoView()` via querySelector
- Applying highlight styles

### Context Menus

**Use base-ui Menu component** with contextMenu trigger mode.

**Shift+right-click bypass:** Check `e.shiftKey` in onContextMenu handler, return early to allow browser default.

**Menu structure:**
```tsx
<Menu>
  <Trigger>{children}</Trigger>
  <Portal>
    <Positioner>
      <Popup>
        {/* items based on context */}
        <Item disabled className="hint">(hold shift for browser menu)</Item>
      </Popup>
    </Positioner>
  </Portal>
</Menu>
```

**Context types and actions:**

| Context | Component | Actions |
|---------|-----------|---------|
| placeholder | Placeholder | "Move entity view here", "Open new view here" |
| child-owning | Child (OutPortal case) | "Move to new tab", "Open another in new tab" |
| root | EntityPanel wrapper | "Open another view in new tab" |
| ref | RefLink | "Open in new tab", "Jump to existing view" (disabled if none) |

### New ViewRegistry Methods

```typescript
// Query
findViewPanel(viewId: ViewId): PanelId | null;
getViewsForEntity(entityId: EntityId): View[];  // Already exists internally, make public

// Navigation
jumpToView(viewId: ViewId): void;

// Highlight
highlightView(viewId: ViewId, durationMs?: number): void;
getHighlightedViewId(): ViewId | null;
subscribeHighlight(listener: () => void): () => void;

// View manipulation (for context menu actions)
moveViewToSlot(viewId: ViewId, targetSlotId: SlotId): void;
moveViewToNewPanel(viewId: ViewId): void;
replacePlaceholderWithNewView(slotId: SlotId): void;  // Only for placeholder slots
// createViewInNewPanel = openEntity (already exists)
```

### View Manipulation Logic

**moveViewToSlot(viewId, targetSlotId):**
1. Get view, verify it exists
2. If view is panel-owned: close that panel (remove from dockview), remove panel entry
3. Update view: `ownerSlotId = targetSlotId`, `panelId = null`
4. Notify: `notifyViews()`, `notifySlotsForEntity(entityId)`

**moveViewToNewPanel(viewId):**
1. Get view, verify it exists and is slot-owned
2. Create new panel entry
3. Update view: `ownerSlotId = null`, `panelId = newPanelId`
4. Add panel to dockview
5. Notify: `notifyViews()`, `notifySlotsForEntity(entityId)`
6. The slot that was owning becomes a placeholder (automatic via SlotContent computation)

**replacePlaceholderWithNewView(slotId):**
1. Get slot, verify it exists
2. Verify slot is a placeholder (no view has `ownerSlotId === slotId`)
3. Get entityId from slot
4. Create new View with `ownerSlotId = slotId`, `panelId = null`
5. The old view(s) for this entity stay where they are - now there are multiple views
6. Notify: `notifyViews()`, `notifySlotsForEntity(entityId)`

### Component Changes

**Child.tsx:** Wrap in context menu. Pass context based on whether it's showing OutPortal or Placeholder.

**Placeholder:** Already inside Child, just needs the menu items.

**EntityPanel.tsx:** Wrap OutPortal in context menu with `type: 'root'`.

**RefLink (in EntityRenderer.tsx):** Wrap in context menu with `type: 'ref'`.

### Implementation Order

1. Add `data-view-id` to EntityRenderer wrapper
2. Add highlight state + methods to ViewRegistry
3. Add `useIsHighlighted` hook, apply CSS in EntityRenderer
4. Add `findViewPanel`, `jumpToView` to ViewRegistry
5. Update `openEntity` to call `jumpToView` when view exists
6. Add view manipulation methods (`moveViewToSlot`, `moveViewToNewPanel`, `createViewInSlot`)
7. Create `ViewContextMenu` component using base-ui
8. Wrap Child, Placeholder, EntityPanel, RefLink with context menus
9. Wire up menu actions to registry methods
