# Portal Management Plan

## Understanding the POC

The proof-of-concept (`poc-singleton-portal.jsx`) establishes a pattern:

1. **External Store**: Uses `useSyncExternalStore` to manage state outside React's render cycle
   - Avoids render-during-render issues when registering in useEffect
   - Fine-grained subscriptions (only affected components re-render)

2. **Registry Structure**:
   ```
   entries: Map<singletonId, {
     portalNode: HtmlPortalNode,
     activeInstanceId: string | null,
     instances: Map<instanceId, { ref }>
   }>
   ```

3. **Two Components**:
   - `SingletonContent`: Defines WHAT (renders InPortal, defined once at top level)
   - `SingletonSlot`: Defines WHERE (renders OutPortal or placeholder, many can exist)

4. **Behavior**: First slot to mount becomes active. Others show placeholder with "take ownership" / "jump to active" options.

## Key Differences for Entity Rendering

The POC assumes **one content, many competing slots**. Our spec has different requirements:

| POC | Entity Rendering |
|-----|------------------|
| One InPortal per ID | Multiple views (InPortals) per entity when copied |
| Slots compete for one content | Child slots show placeholder if ANY view exists |
| All slots equal | Panel views have priority over inline views |
| Explicit "take ownership" | Default is placeholder; explicit "move" or "copy" |

## Core Question: When Does Child Create a View?

The spec says: when a renderer uses `<Child entityId={X}>`:
- If entity X has existing view(s) → show placeholder
- If no existing views → create inline view, show OutPortal

But what about **top-level panels**? They can't show placeholders. They must always show content.

**Key insight**: There are two ways to "request" an entity be rendered:

1. **Panel request** (explicit): User opens entity → always creates a view
2. **Child request** (implicit): Renderer renders child → creates view only if none exists

---

## Thinking Through the Model

### Option A: Unified Slot Model (POC-style)

Every place that wants to show an entity registers as a "slot":
- Panels register as slots
- Child components register as slots
- Slots have priority levels (panel > inline)
- Highest-priority slot gets the content, others get placeholder

```
registry: Map<entityId, {
  portalNode: HtmlPortalNode,
  activeSlotId: string | null,
  slots: Map<slotId, { priority, location, ref }>
}>
```

**Problem**: When you "copy" an entity, now you have two portals for the same entity. This breaks the "one portalNode per entityId" assumption.

### Option B: View-Centric Model

Each "view" is a distinct rendered instance with its own InPortal:

```
views: Map<viewId, {
  entityId,
  portalNode,
  location: 'panel' | { parentViewId }
}>
viewsByEntity: Map<entityId, Set<viewId>>
```

Child component behavior:
- Look up `viewsByEntity[entityId]`
- If non-empty → show placeholder pointing to first view
- If empty → create new view (inline), show OutPortal

Panel behavior:
- Always create a new view when opening
- Unless... should opening an entity that exists inline "adopt" the inline view?

**Hmm, spec says**: "when clicking a link or ref, the entity should either become focused (if already rendered) or open in new panel (if not)"

So if entity is rendered inline, clicking it should FOCUS it (scroll to it, highlight it), not open a new panel. Only if not rendered anywhere should it open in panel.

---

## Revised Understanding

Let me re-read the spec more carefully...

From spec:
> "By default, when a newly opened node tries to render a child that is already open somewhere else, that child actually renders as a placeholder"

So "open somewhere else" means ANY existing view, whether panel or inline.

> "Additionally, clicking a ref will by default jump to an open copy."

So clicking a ref doesn't open new panel if entity already has a view.

> "However, if there is no open copy, the node is allowed to render inline."

So Child creates inline view only if no views exist.

> "It is also possible to use a right click on a rendered node or on a panel to 'open a copy'. In this case a new panel opens with a copy of the contents."

Explicit copy action creates new view.

### So the flow is:

**Opening entity (click ref)**:
1. Check if entity has any views
2. If yes → focus that view (scroll/highlight)
3. If no → create new view in panel

**Rendering child**:
1. Check if child entity has any views
2. If yes → render placeholder
3. If no → create inline view, render OutPortal

**Explicit copy**:
- Always creates new view (new InPortal)

---

## How Does This Map to the POC Pattern?

The POC's "slot" concept maps to "places that want to show an entity":
- Panels wanting to show an entity
- Child components wanting to show a child entity

But unlike the POC where slots compete for ONE portal, in our model:
- Multiple views (portals) CAN exist for same entity (when copied)
- By default, only ONE view exists
- When multiple views exist, they each have their own portal (no competition)

So actually the slot registration is simpler:
- No need for "activeInstanceId" - each view IS an instance
- Slots don't compete; they either have their own view or show placeholder

### Revised Registry Design

```typescript
interface ViewRegistry {
  // All views
  views: Map<ViewId, View>;

  // Quick lookup by entity
  viewsByEntity: Map<EntityId, Set<ViewId>>;

  // Track slots that are showing placeholders
  // (so we know where to "move" a view to, or adopt on close)
  placeholders: Map<PlaceholderId, {
    entityId: EntityId,
    location: PlaceholderLocation,
    ref: RefObject
  }>;
}

interface View {
  viewId: ViewId;
  entityId: EntityId;
  portalNode: HtmlPortalNode;
  location: ViewLocation;
}

type ViewLocation =
  | { type: 'panel', panelId: string }
  | { type: 'inline', parentViewId: ViewId }

type PlaceholderLocation =
  | { type: 'panel', panelId: string }  // can this happen? probably not
  | { type: 'inline', parentViewId: ViewId }
```

Wait, can a panel show a placeholder? The spec doesn't mention this. I think panels always show content. So PlaceholderLocation is always inline.

Actually re-reading: "when a newly opened node tries to render a child that is already open somewhere else" - so it's always about CHILDREN showing placeholders, not panels.

Panels are top-level; they always have their own view.

---

## The Child Component in Detail

When `<Child entityId={X} customArgs={...}>` mounts:

```
1. Generate instanceId (useId)
2. Register with RenderManager: "I want to show entity X at location (inline in view Y)"
3. RenderManager checks: does entity X have any views?
   - If yes: Return { type: 'placeholder', existingViewId }
   - If no: Create new view, return { type: 'view', viewId, portalNode }
4. Component renders based on result:
   - placeholder: render placeholder UI
   - view: render OutPortal
```

On unmount:
```
1. Unregister from RenderManager
2. If this was a view (not placeholder):
   - RenderManager checks: are there placeholders wanting this entity?
   - If yes: "adopt" - transfer view to placeholder location
   - If no: destroy view
```

### useSyncExternalStore Pattern

Following the POC, the Child component needs:

```typescript
function Child({ entityId, customArgs }) {
  const manager = useRenderManager();
  const instanceId = useId();
  const parentViewId = useCurrentViewId(); // from context

  // Subscribe to manager state
  const slotInfo = useSyncExternalStore(
    manager.subscribe,
    () => manager.getSlotInfo(entityId, instanceId),
    () => manager.getSlotInfo(entityId, instanceId)
  );

  // Register/unregister in effect
  useEffect(() => {
    manager.registerChildSlot(entityId, instanceId, parentViewId);
    return () => manager.unregisterChildSlot(entityId, instanceId);
  }, [entityId, instanceId, parentViewId]);

  if (slotInfo.type === 'placeholder') {
    return <Placeholder entityId={entityId} targetViewId={slotInfo.existingViewId} />;
  }

  return <OutPortal node={slotInfo.portalNode} />;
}
```

### The Timing Problem

There's a subtle issue: useEffect runs AFTER render. So on first render, the slot isn't registered yet.

The POC handles this by:
1. First render: slot not registered, entry might not exist, shows fallback
2. useEffect runs: registers slot
3. Subscription triggers re-render
4. Second render: now has correct state

We can do the same. First render might show placeholder (or nothing), then settles.

But wait - if on first render we always show placeholder, then register, then maybe get upgraded to view... that causes a flash.

**Better approach**: Check synchronously during render whether we SHOULD be a view or placeholder, based on current registry state. The useEffect registration is for tracking and cleanup, not for determining state.

```typescript
function Child({ entityId }) {
  const manager = useRenderManager();
  const instanceId = useId();
  const parentViewId = useCurrentViewId();

  // Synchronously determine state
  const existingViews = manager.getViewsForEntity(entityId);
  const shouldBeView = existingViews.length === 0;

  // Get or create view synchronously if needed
  const viewInfo = useMemo(() => {
    if (shouldBeView) {
      return manager.getOrCreateInlineView(entityId, instanceId, parentViewId);
    }
    return null;
  }, [shouldBeView, entityId, instanceId, parentViewId]);

  // Register for tracking/cleanup
  useEffect(() => {
    if (viewInfo) {
      manager.trackInlineView(viewInfo.viewId, instanceId);
    } else {
      manager.trackPlaceholder(entityId, instanceId, parentViewId);
    }
    return () => manager.untrack(instanceId);
  }, [viewInfo, entityId, instanceId, parentViewId]);

  if (!viewInfo) {
    return <Placeholder entityId={entityId} targetViewId={existingViews[0]} />;
  }

  return <OutPortal node={viewInfo.portalNode} />;
}
```

Hmm but `useMemo` with side effects (creating view) is not ideal...

---

## Alternative: Declarative View Creation

What if we separate "declaring intent" from "rendering content"?

RenderManager maintains the source of truth. Child components declare their intent and subscribe to the result.

```typescript
// In RenderManager, during render (not effect):
function processChildIntents() {
  // For each entity, look at all pending "wants to render" intents
  // Decide which one(s) get views vs placeholders
  // This is pure computation based on current state
}
```

But this still has the problem of registering intent during render...

---

## Looking at POC More Carefully

The POC's `useSingletonSlot` hook does this:

```javascript
// Registration happens in useEffect
useEffect(() => {
  registry.register(id, instanceId, containerRef);
  return () => registry.unregister(id, instanceId);
}, [id, instanceId, registry]);

// State is read via useSyncExternalStore
const entry = useSyncExternalStore(
  registry.subscribe,
  () => registry.getEntry(id),
);

const isActive = entry?.activeInstanceId === instanceId;
```

So on first render:
- entry might be undefined or might have other instances
- instanceId is not yet registered
- `isActive` will be false (since our instanceId isn't registered)

After useEffect:
- register is called
- notify fires
- component re-renders
- now `isActive` might be true

So there IS a two-render cycle. First render shows fallback, second render shows content (if active).

For our case, this means:
- First render: Child shows placeholder (because its instanceId isn't registered and no view exists for it)
- After effect: registers, creates view if appropriate, re-renders
- Second render: Child shows content (OutPortal) or placeholder

This is acceptable. The flicker is minimal and only happens on mount.

---

## Handling the InPortal

Where do the InPortals actually render? In the POC, `SingletonContent` is a separate component that renders the InPortal at the top level.

For our entity system:
- When a view is created, we need to render its InPortal somewhere
- The InPortals should be in a hidden container
- RenderManager owns this container

```typescript
function RenderManager({ data }) {
  const [views, setViews] = useState<Map<ViewId, View>>(new Map());

  return (
    <>
      {/* Hidden InPortal container */}
      <div style={{ display: 'none' }}>
        {Array.from(views.values()).map(view => (
          <InPortal key={view.viewId} node={view.portalNode}>
            <ViewContextProvider viewId={view.viewId} entityId={view.entityId}>
              <EntityRenderer entityId={view.entityId} />
            </ViewContextProvider>
          </InPortal>
        ))}
      </div>

      {/* Dockview with panels */}
      <DockviewReact ... />
    </>
  );
}
```

But wait - if we use useState for views, then adding a view triggers a re-render of RenderManager. That's fine, but we need to be careful about when views are created.

Actually the POC uses an external store precisely to avoid this. The registry is mutable; it doesn't cause RenderManager to re-render. Individual components subscribe and re-render independently.

For the InPortal rendering, we'd need RenderManager to subscribe to the view list:

```typescript
function RenderManager({ data }) {
  const registry = useRegistry();

  const views = useSyncExternalStore(
    registry.subscribe,
    () => registry.getViews()
  );

  return (
    <>
      <div style={{ display: 'none' }}>
        {Array.from(views.values()).map(view => (
          <InPortal key={view.viewId} node={view.portalNode}>
            <EntityRenderer entityId={view.entityId} />
          </InPortal>
        ))}
      </div>
      ...
    </>
  );
}
```

This way, when a new view is created (e.g., by a Child component's effect), the registry updates, RenderManager re-renders, and the new InPortal appears.

---

## Top-Level Entity Panel

EntityPanel needs to show an entity. Unlike Child, it can't show a placeholder.

Options:

**Option 1**: EntityPanel always creates a new view on mount

```typescript
function EntityPanel({ params }) {
  const { entityId } = params;
  const registry = useRegistry();
  const viewId = useId();

  // Ensure view exists
  useEffect(() => {
    registry.createView(entityId, viewId, { type: 'panel', panelId });
    return () => registry.destroyView(viewId);
  }, [entityId, viewId]);

  const view = useSyncExternalStore(
    registry.subscribe,
    () => registry.getView(viewId)
  );

  if (!view) return <Loading />;
  return <OutPortal node={view.portalNode} />;
}
```

**Option 2**: EntityPanel receives viewId as param (RenderManager creates view when opening panel)

```typescript
// When opening entity panel:
const viewId = registry.createView(entityId, { type: 'panel', panelId });
dockviewApi.addPanel({
  component: 'entity',
  params: { viewId }
});

// EntityPanel just renders the OutPortal for the given viewId
function EntityPanel({ params }) {
  const { viewId } = params;
  const registry = useRegistry();

  const view = useSyncExternalStore(
    registry.subscribe,
    () => registry.getView(viewId)
  );

  return <OutPortal node={view.portalNode} />;
}
```

Option 2 is cleaner - the view is created before the panel opens, so there's no loading state.

---

## Interaction: Opening an Entity

When user clicks a ref:

```typescript
function openEntity(entityId: EntityId) {
  const existingViews = registry.getViewsForEntity(entityId);

  if (existingViews.length > 0) {
    // Focus existing view
    const viewId = existingViews[0];
    const view = registry.getView(viewId);
    if (view.location.type === 'panel') {
      dockviewApi.getPanel(view.location.panelId)?.focus();
    } else {
      // Inline view - scroll to it? How?
      // Need a ref to the OutPortal container
      // The registry could store refs...
    }
  } else {
    // Create new view in panel
    const viewId = generateViewId();
    const panelId = `entity-${viewId}`;
    registry.createView(entityId, viewId, { type: 'panel', panelId });
    dockviewApi.addPanel({
      id: panelId,
      component: 'entity',
      params: { viewId },
      title: getEntityTitle(entityId)
    });
  }
}
```

---

## Interaction: Move Entity Here (from placeholder)

Right-click placeholder → "Move entity here"

```typescript
function moveEntityHere(entityId: EntityId, targetLocation: InlineLocation) {
  const existingViews = registry.getViewsForEntity(entityId);
  const viewId = existingViews[0]; // the view we're moving

  // Update view's location
  registry.updateViewLocation(viewId, targetLocation);

  // If it was in a panel, close that panel
  const oldLocation = registry.getView(viewId).location;
  if (oldLocation.type === 'panel') {
    dockviewApi.getPanel(oldLocation.panelId)?.close();
  }

  // The placeholder at targetLocation will now see that it "owns" this view
  // and render OutPortal instead of placeholder
}
```

Wait, this requires the placeholder to know it "owns" the view after the move. But how?

Currently, Child component checks "does entity have views?" - if yes, placeholder.

After move, entity still has views, but the view's location matches this Child's location...

So Child component needs to check: "does entity have a view AT MY LOCATION?"

```typescript
function Child({ entityId }) {
  const instanceId = useId();
  const parentViewId = useCurrentViewId();
  const myLocation = { type: 'inline', parentViewId, instanceId };

  const viewAtMyLocation = registry.getViewAtLocation(entityId, myLocation);

  if (viewAtMyLocation) {
    return <OutPortal node={viewAtMyLocation.portalNode} />;
  }

  const existingViews = registry.getViewsForEntity(entityId);
  if (existingViews.length > 0) {
    return <Placeholder entityId={entityId} />;
  }

  // No views exist - I should create one
  // But this needs to be in effect...
}
```

This is getting complex. Let me think about the data model more carefully.

---

## Revised Data Model

```typescript
interface View {
  viewId: ViewId;
  entityId: EntityId;
  portalNode: HtmlPortalNode;
  location: ViewLocation;
}

type ViewLocation =
  | { type: 'panel', panelId: string }
  | { type: 'inline', parentViewId: ViewId, slotId: string }

// slotId is the Child component's instanceId
```

Registry operations:
- `createView(entityId, viewId, location)` - creates new view
- `destroyView(viewId)` - removes view
- `moveView(viewId, newLocation)` - updates location
- `getViewsForEntity(entityId)` - returns all views for entity
- `getViewAtLocation(location)` - returns view at specific location, if any

Child component logic:
```typescript
function Child({ entityId }) {
  const slotId = useId();
  const parentViewId = useCurrentViewId();
  const myLocation = { type: 'inline', parentViewId, slotId };

  // Check if there's a view at my location
  const myView = registry.getViewAtLocation(myLocation);

  // Check if there are any views for this entity
  const allViews = registry.getViewsForEntity(entityId);

  // Register this slot (for placeholder tracking)
  useEffect(() => {
    registry.registerSlot(entityId, slotId, myLocation);
    return () => registry.unregisterSlot(slotId);
  }, []);

  if (myView) {
    // I own a view at my location
    return <OutPortal node={myView.portalNode} />;
  }

  if (allViews.length > 0) {
    // Views exist elsewhere
    return <Placeholder entityId={entityId} targetViewId={allViews[0]} />;
  }

  // No views exist - should I create one?
  // This is tricky - we can't create during render
  // But if we wait for effect, there might be a flash

  // Option: Render placeholder initially, create in effect, re-render with view
  return <Placeholder entityId={entityId} creating={true} />;
}
```

And in the effect:
```typescript
useEffect(() => {
  const allViews = registry.getViewsForEntity(entityId);
  if (allViews.length === 0) {
    // Create view at my location
    const viewId = generateViewId();
    registry.createView(entityId, viewId, myLocation);
  }
  // ... cleanup
}, [entityId, myLocation]);
```

The "creating" placeholder would be very brief - just one render cycle.

---

## Multiple Copies

When user explicitly requests a copy:

```typescript
function copyEntityHere(entityId: EntityId, targetLocation: Location) {
  const viewId = generateViewId();
  registry.createView(entityId, viewId, targetLocation);

  // If targetLocation is a panel, open the panel
  if (targetLocation.type === 'panel') {
    dockviewApi.addPanel({ ... });
  }
  // If inline, the Child at that location will pick it up on next render
}
```

After this, `getViewsForEntity(entityId)` returns multiple views. Each Child component checks if there's a view at ITS location specifically.

---

## Panel Close → Orphan Adoption

When a panel closes:

```typescript
function onPanelClose(panelId: string) {
  const view = registry.getViewAtPanelLocation(panelId);
  if (!view) return;

  // Is this the last view for this entity?
  const allViews = registry.getViewsForEntity(view.entityId);
  if (allViews.length === 1) {
    // Check if there are placeholders wanting this entity
    const placeholders = registry.getPlaceholdersForEntity(view.entityId);
    if (placeholders.length > 0) {
      // Adopt: move view to placeholder's location
      registry.moveView(view.viewId, placeholders[0].location);
      return;
    }
  }

  // No adoption needed, just destroy
  registry.destroyView(view.viewId);
}
```

---

## Summary of Registry API

```typescript
interface ViewRegistry {
  // Views
  createView(entityId: EntityId, viewId: ViewId, location: ViewLocation): void;
  destroyView(viewId: ViewId): void;
  moveView(viewId: ViewId, newLocation: ViewLocation): void;
  getView(viewId: ViewId): View | undefined;
  getViewsForEntity(entityId: EntityId): View[];
  getViewAtLocation(location: ViewLocation): View | undefined;

  // Placeholders (slots without views)
  registerPlaceholder(entityId: EntityId, slotId: string, location: InlineLocation): void;
  unregisterPlaceholder(slotId: string): void;
  getPlaceholdersForEntity(entityId: EntityId): Placeholder[];

  // Subscription
  subscribe(listener: () => void): () => void;
  getSnapshot(): RegistryState;
}
```

---

## Open Questions

1. **View creation timing**: Creating view in useEffect means brief placeholder flash. Acceptable?

2. **Location identity**: How to identify inline locations? `{ parentViewId, slotId }` should be unique. But what if a renderer renders the same child twice? Each would have different slotId (from useId), so they'd be different locations. Is that right? I think so - each slot is distinct.

3. **Focusing inline views**: When clicking ref for entity with inline view, how do we scroll to it? Need refs stored in registry.

4. **Multiple copies at same location?**: Can two views exist at the same inline location? Probably not - we should enforce uniqueness.

5. **What if panel and inline both want same entity?**: Panel was opened, then renderer tries to render as child. The child shows placeholder (correct). But what if panel closes? The orphan adoption should move view to child location.

---

## Rethinking: Simpler Model?

I wonder if I'm overcomplicating this. Let me think about what the POC actually does vs what we need...

**POC model (simplified)**:
- One "content" per ID, defined at top level
- Multiple "slots" compete to display it
- First-registered slot wins, others show fallback
- Slots can "take ownership" (become active)

**What if we adapted this more directly?**

Instead of "views" being created dynamically, what if:
- EntityRenderer for each entity is always rendered (in hidden InPortal container)
- Slots compete to display it
- Panel slots have priority over inline slots

```
registry: Map<entityId, {
  portalNode: HtmlPortalNode,
  slots: Map<slotId, SlotInfo>,
  activeSlotId: string | null
}>
```

When a Child component mounts:
1. Register as slot for this entity
2. Check if I'm the active slot
3. If yes: render OutPortal
4. If no: render placeholder

Active slot determination:
- If any panel slot exists → it's active
- Else if any inline slots → first registered is active

**Problem**: This doesn't handle copies. If user wants two copies, we need two InPortals.

**Solution**: Separate "singleton" entities from "copied" entities:

```
entities: Map<entityId, {
  // Default (singleton) instance
  singletonPortal: HtmlPortalNode,
  singletonSlots: Map<slotId, SlotInfo>,
  activeSingletonSlot: string | null,

  // Additional copies (when user explicitly creates)
  copies: Map<copyId, {
    portalNode: HtmlPortalNode,
    location: ViewLocation
  }>
}>
```

Hmm, this is getting complex again. Let me think differently...

---

## Alternative: Lazy InPortal Creation

What if we DON'T pre-render all entity content? Instead:
- InPortal is created lazily when first needed
- Once created, it persists (for state preservation)
- Multiple copies = multiple InPortals

This is closer to my earlier "View-Centric Model". Each View has its own InPortal.

The question is: when is "first needed"?

**For panels**: When panel opens, create View (and InPortal)
**For inline**: When Child determines it should render content (not placeholder)

The timing issue is: Child runs render, sees no views exist, decides it should create one... but creating a View causes RenderManager to re-render (to add InPortal), which might cause issues.

**Solution: Two-phase registration**

Phase 1 (during render):
- Child checks registry state
- If entity has views: render placeholder
- If no views: render "pending view" placeholder, record intent

Phase 2 (during effect):
- Process pending intents
- Create Views as needed
- Notify for re-render

Phase 3 (next render):
- Child now sees View at its location
- Renders OutPortal

This is basically what the POC does! The "flash" is just one render cycle where the slot doesn't have content yet.

Actually, we could eliminate the flash by using `useLayoutEffect` instead of `useEffect` for registration - it runs synchronously before paint.

---

## Simplified Design (Final?)

### Registry State

```typescript
interface RegistryState {
  views: Map<ViewId, View>;
  slots: Map<SlotId, Slot>;  // registered slots (for placeholder tracking)
}

interface View {
  viewId: ViewId;
  entityId: EntityId;
  portalNode: HtmlPortalNode;
  ownerSlotId: SlotId | null;  // which slot "owns" this view (null if panel)
  panelId: string | null;       // which panel (null if inline)
}

interface Slot {
  slotId: SlotId;
  entityId: EntityId;
  parentViewId: ViewId;  // which view contains this slot
}
```

### Child Component Flow

```typescript
function Child({ entityId }) {
  const registry = useContext(RegistryContext);
  const slotId = useId();
  const parentViewId = useCurrentViewId();

  // Subscribe to relevant state
  const { myView, hasOtherViews } = useSyncExternalStore(
    registry.subscribe,
    () => {
      const views = registry.getViewsForEntity(entityId);
      const myView = views.find(v => v.ownerSlotId === slotId);
      return {
        myView,
        hasOtherViews: views.length > 0 && !myView
      };
    }
  );

  // Register slot and potentially create view
  useLayoutEffect(() => {
    registry.registerSlot(slotId, entityId, parentViewId);

    // If no views exist, create one for this slot
    const views = registry.getViewsForEntity(entityId);
    if (views.length === 0) {
      registry.createInlineView(entityId, slotId, parentViewId);
    }

    return () => {
      registry.unregisterSlot(slotId);
      // If this slot owned a view, handle cleanup/adoption
    };
  }, [slotId, entityId, parentViewId]);

  if (myView) {
    return <OutPortal node={myView.portalNode} />;
  }

  return <Placeholder entityId={entityId} onMove={() => ...} />;
}
```

### RenderManager

```typescript
function RenderManager({ data }) {
  const registry = useRef(createRegistry()).current;

  // Subscribe to views for rendering InPortals
  const views = useSyncExternalStore(
    registry.subscribe,
    () => Array.from(registry.state.views.values())
  );

  return (
    <RegistryContext.Provider value={registry}>
      {/* InPortal container */}
      <div style={{ display: 'none' }}>
        {views.map(view => (
          <InPortal key={view.viewId} node={view.portalNode}>
            <CurrentViewContext.Provider value={view.viewId}>
              <EntityRenderer entityId={view.entityId} data={data} />
            </CurrentViewContext.Provider>
          </InPortal>
        ))}
      </div>

      {/* Dockview */}
      <DockviewReact ... />
    </RegistryContext.Provider>
  );
}
```

### Opening Entity in Panel

```typescript
function openEntity(entityId: EntityId) {
  const views = registry.getViewsForEntity(entityId);

  if (views.length > 0) {
    // Focus existing
    focusView(views[0]);
    return;
  }

  // Create panel view
  const viewId = registry.createPanelView(entityId);
  const panelId = `entity-${viewId}`;
  dockviewApi.addPanel({
    id: panelId,
    component: 'entity',
    params: { viewId },
  });
}
```

---

## User Interactions Summary

| Action | Result |
|--------|--------|
| Click ref (no existing view) | Create panel view, open panel |
| Click ref (existing view) | Focus existing view |
| Right-click placeholder → "Move here" | Change view's ownerSlotId to this slot |
| Right-click placeholder → "Open copy" | Create new view at this slot location |
| Right-click view → "Open copy in panel" | Create new panel view |
| Close panel (last view, has placeholder) | Move view to placeholder location |
| Close panel (last view, no placeholder) | Destroy view |
| Close panel (not last view) | Destroy view |

---

## Edge Case: Panel Opens for Entity with Inline View

Scenario:
1. Entity E is rendered inline (inside parent P)
2. User clicks ref to E (wanting to open in panel)

Options:

**A. Move inline view to panel**
- The inline view becomes a panel view
- Parent P now shows placeholder
- Entity state is preserved (same InPortal)

**B. Create new panel view**
- Now two views exist for E
- Inline view stays where it is
- Panel has its own view (new InPortal, fresh state)
- This is a "copy" scenario

Per spec: "clicking a ref will by default jump to an open copy"

So default should be: if E has inline view, focus it (scroll to parent P, highlight E).

But if user explicitly wants E in a panel? They'd need to right-click → "Open in panel", which would do option A or B depending on preference.

I think **A** makes more sense as default for "open in panel". The spec says copies are explicit, so opening in panel should move, not copy.

**Actually re-reading spec:**
> "when clicking a link or ref, the entity being clicked on should either become focused (if it is already rendered somewhere) or open in a new panel (if not)"

So clicking ref = focus if exists, else open panel. User never explicitly "opens in panel" unless entity doesn't exist. The "open a copy" action is for creating duplicates.

So the flow is:
- Click ref to E (has inline view) → focus inline view
- Right-click E → "Open copy in panel" → creates new panel view (copy)

No need for "move to panel" action? Actually there might be:
- Right-click inline view → "Open in own panel" → moves view to panel
- Right-click placeholder → "Move entity here" → moves view to this slot

---

## Inline View Hierarchy and Cleanup

When a parent view is destroyed, what happens to its inline children?

Example:
- Panel P1 shows entity E1
- E1's renderer shows child E2 inline
- User closes panel P1

When P1 closes:
1. View for E1 is destroyed (or adopted if placeholder exists)
2. What about the view for E2?

E2's view was inline in E1. When E1's view is destroyed, E2's InPortal is unmounted (because it was rendered inside E1's context).

Wait, no - InPortals are all rendered in RenderManager's hidden container, not inside each other. The OutPortal for E2 was inside E1's renderer, but the InPortal (actual content) was in RenderManager.

So when E1's view is destroyed:
- E1's InPortal is removed from RenderManager
- E1's OutPortal (in panel P1) is unmounted
- But E2's InPortal is still in RenderManager!
- E2's OutPortal (which was inside E1's renderer) is gone

Now E2 has a view but no OutPortal displaying it. It's orphaned.

We need to track parent-child relationships and clean up children when parent is destroyed.

```typescript
interface View {
  viewId: ViewId;
  entityId: EntityId;
  portalNode: HtmlPortalNode;
  parentViewId: ViewId | null;  // null for panel views
  panelId: string | null;
}
```

When destroying a view:
1. Find all child views (where parentViewId === this viewId)
2. Recursively handle each child:
   - If child has placeholder elsewhere → move there (adoption)
   - Else → destroy child

---

## Summary of Key Decisions

1. **View model**: Each View has its own InPortal. Views are created lazily.

2. **Singleton by default**: An entity typically has one view. Copies are explicit.

3. **Child component**: Registers as slot, creates view if none exists, else shows placeholder.

4. **Panel views**: Created when opening entity in panel. Always show content.

5. **Inline views**: Created by Child component when no other views exist.

6. **Click ref**: Focus existing view, or create panel view if none.

7. **Move vs copy**: "Move" transfers ownership of existing view. "Copy" creates new view.

8. **Cleanup**: Destroying a view destroys its children (or adopts them).

9. **useLayoutEffect**: Use for registration to minimize render flicker.

10. **External store**: Registry uses external store pattern with useSyncExternalStore.

---

## Remaining Questions for User

1. When clicking ref to inline entity, how should we "focus" it? Scroll + highlight? Flash the border?

2. Should "Open in panel" action exist for inline views (moves to panel), or only "Open copy"?

3. What visual treatment for placeholders? The POC has buttons; spec mentions showing as one-line with "[entity] open elsewhere".

4. Should the debug panel show the registry state (views, slots) in addition to entity data?

---

## Next Steps

1. Implement ViewRegistry with external store pattern
2. Implement RenderManager integration (InPortal rendering, panel opening)
3. Implement Child component with placeholder/view logic
4. Implement EntityPanel
5. Wire up interactions (click to open/focus, right-click menu)
6. Test orphan adoption on panel close
