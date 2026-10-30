# Phase 2 Step 9: Persistent Renderer State

## Overview

Add a `usePersistentThingState` hook that allows custom renderers to save state that persists across component re-mounts within a session. Multiple views of the same thing share the same state.

## Goals

1. **Session-persistent state** - State survives component unmount/remount but is lost on page refresh
2. **Shared across views** - Same thing opened in multiple panels shares state
3. **Simple API** - Hook returns `[value, setValue]` like useState, but keyed by thing + user key
4. **Undefined optimization** - Setting to undefined removes from storage (cleanup)

## API Design

### Hook Signature

```typescript
function usePersistentThingState<T>(key: string): [T | undefined, (value: T | undefined) => void];
```

- `key`: User-provided string to namespace their state (e.g., "scrollPosition", "expandedSections")
- Returns tuple of current value and setter
- Always starts as `undefined` - no initial value parameter
- Setting to `undefined` clears the state from storage

### Storage Key Format

```
${shortId}:${userKey}
```

Where `shortId` is the thing's short ID (from renderer context) and `userKey` is the user-provided key.

### Usage Example

```tsx
import React from 'react';
import { usePersistentThingState } from 'thingwatcher-bindings';

export default function MyRenderer({ payload }) {
  // Persists scroll position across re-mounts
  const [scrollY, setScrollY] = usePersistentThingState<number>("scroll");

  // Persists expanded sections
  const [expanded, setExpanded] = usePersistentThingState<Set<string>>("expanded");

  return (
    <div
      onScroll={(e) => setScrollY(e.currentTarget.scrollTop)}
      style={{ transform: `translateY(-${scrollY ?? 0}px)` }}
    >
      {payload.sections.map(section => (
        <Section
          key={section.id}
          expanded={expanded?.has(section.id) ?? false}
          onToggle={() => {
            const next = new Set(expanded ?? []);
            if (next.has(section.id)) next.delete(section.id);
            else next.add(section.id);
            setExpanded(next.size > 0 ? next : undefined); // Clear if empty
          }}
        />
      ))}
    </div>
  );
}
```

## Architecture

### State Store

A global in-memory store that holds all persistent state:

```typescript
// persistentStateStore.ts

type Listener = () => void;

class PersistentStateStore {
  private state = new Map<string, unknown>();
  private listeners = new Map<string, Set<Listener>>();

  get<T>(key: string): T | undefined {
    return this.state.get(key) as T | undefined;
  }

  set<T>(key: string, value: T | undefined): void {
    if (value === undefined) {
      this.state.delete(key);
    } else {
      this.state.set(key, value);
    }
    // Notify listeners for this key
    this.listeners.get(key)?.forEach(fn => fn());
  }

  subscribe(key: string, listener: Listener): () => void {
    if (!this.listeners.has(key)) {
      this.listeners.set(key, new Set());
    }
    this.listeners.get(key)!.add(listener);
    return () => {
      this.listeners.get(key)?.delete(listener);
      if (this.listeners.get(key)?.size === 0) {
        this.listeners.delete(key);
      }
    };
  }
}

export const persistentStateStore = new PersistentStateStore();
```

### Hook Implementation

```typescript
// In ThingWatcherBindings.tsx

import { useSyncExternalStore, useCallback } from 'react';
import { persistentStateStore } from './persistentStateStore';

export function usePersistentThingState<T>(userKey: string): [T | undefined, (value: T | undefined) => void] {
  const ctx = useRendererContext();

  // Build storage key from thing's shortId + user key
  // We need shortId in context - this requires passing it through
  const shortId = ctx.currentThingShortId;
  if (shortId === undefined) {
    throw new Error('usePersistentThingState must be used within a renderer context with a thing');
  }

  const storageKey = `${shortId}:${userKey}`;

  // Subscribe to store changes using useSyncExternalStore
  const value = useSyncExternalStore(
    (onStoreChange) => persistentStateStore.subscribe(storageKey, onStoreChange),
    () => persistentStateStore.get<T>(storageKey),
  );

  const setValue = useCallback((newValue: T | undefined) => {
    persistentStateStore.set(storageKey, newValue);
  }, [storageKey]);

  return [value, setValue];
}
```

### Context Changes

Need to add `currentThingShortId` to `RendererContextValue`:

```typescript
export interface RendererContextValue {
  client: Client;
  currentTick: number;
  renderers: Map<string, string>;
  onSelectThing: (shortId: number) => void;
  onContextMenu: (event: { clientX: number; clientY: number }, shortId: number) => void;
  triggerPause: (shortId: number, payload: unknown) => Promise<TriggerResult>;
  renderDepth: number;
  currentThingShortId?: number;  // NEW: For usePersistentThingState
}
```

This needs to be set when rendering:
- In `DetailsPanel` when rendering custom renderer
- In `ThingView` when rendering nested things
- In `EmbedApp` when rendering the single thing

### Passing shortId Through Context

The tricky part is that `RendererContext` is created once at the top level (in `DockviewLayout` or `EmbedApp`), but `currentThingShortId` changes per-thing being rendered.

Solution: Create a nested provider or use a separate context for the current thing:

```typescript
// ThingContext for current thing being rendered
const ThingContext = createContext<{ shortId: number } | null>(null);

export function ThingProvider({ shortId, children }: { shortId: number; children: ReactNode }) {
  return <ThingContext.Provider value={{ shortId }}>{children}</ThingContext.Provider>;
}

// Hook to get current thing's shortId
function useCurrentThingShortId(): number {
  const ctx = useContext(ThingContext);
  if (!ctx) {
    throw new Error('usePersistentThingState must be used within a ThingProvider');
  }
  return ctx.shortId;
}
```

Then wrap renderers with `ThingProvider`:

```tsx
// In DetailsPanel
<ThingProvider shortId={thing.shortId}>
  <DynamicRenderer ... />
</ThingProvider>

// In ThingView (for nested rendering)
<ThingProvider shortId={thingData.shortId}>
  <RenderComponent ... />
</ThingProvider>
```

## Implementation Details

### File Changes

1. **New file: `frontend/lib/persistentStateStore.ts`**
   - `PersistentStateStore` class with get/set/subscribe
   - Singleton export `persistentStateStore`

2. **Update: `frontend/lib/ThingWatcherBindings.tsx`**
   - Add `ThingContext` and `ThingProvider`
   - Add `usePersistentThingState` hook
   - Export new items

3. **Update: `frontend/components/DetailsPanel/DetailsPanel.tsx`**
   - Wrap `DynamicRenderer` with `ThingProvider`

4. **Update: `frontend/components/DockviewLayout/ThingViewPanel.tsx`**
   - Wrap `DetailsPanel` with `ThingProvider` (if not already wrapped by DetailsPanel)

5. **Update: `frontend/lib/ThingWatcherBindings.tsx` (ThingView component)**
   - Wrap recursive renderer with `ThingProvider`

6. **Update: `frontend/EmbedApp.tsx`**
   - Wrap renderer with `ThingProvider`

### Injection into Dynamic Renderers

The `usePersistentThingState` hook needs to be available in dynamically compiled renderers. It's already exported from `thingwatcher-bindings`, so it will be available via:

```tsx
import { usePersistentThingState } from 'thingwatcher-bindings';
```

The esbuild plugin already injects `thingwatcher-bindings` as a module.

## Edge Cases

### Same key used twice in same renderer

If a renderer calls `usePersistentThingState("foo")` twice, both calls get the same value. This is fine and expected - it's keyed by `${shortId}:foo`.

### Renderer re-mount vs thing change

- **Re-mount same thing**: State preserved (that's the point)
- **Different thing**: Different shortId → different storage key → fresh undefined

### ThingView nested rendering

When `ThingView` renders a child thing, it wraps with a new `ThingProvider` with the child's shortId. So nested renderers have their own state namespace.

### Memory management

State persists indefinitely unless set to undefined. This is intentional - user might navigate away and back. For long sessions with many things, this could accumulate. Acceptable for now; could add LRU eviction later if needed.

## Testing

Manual testing with a demo renderer:

```tsx
// Register a test renderer that uses persistent state
register_renderer("TestWidget", `
import React from 'react';
import { usePersistentThingState } from 'thingwatcher-bindings';

export default function TestWidget({ payload }) {
  const [count, setCount] = usePersistentThingState<number>("count");

  return (
    <div>
      <p>Count: {count ?? 0}</p>
      <button onClick={() => setCount((count ?? 0) + 1)}>Increment</button>
      <button onClick={() => setCount(undefined)}>Clear</button>
    </div>
  );
}
`);
```

Test scenarios:
1. Increment count, close panel, reopen → count preserved
2. Open same thing in two panels → both show same count, incrementing one updates other
3. Click Clear → state removed, both panels show 0
4. Different things → independent state

## Implementation Steps

- [x] 1. Create `persistentStateStore.ts` with store class
- [x] 2. Add `ThingContext` and `ThingProvider` to ThingWatcherBindings
- [x] 3. Implement `usePersistentThingState` hook
- [x] 4. Update DetailsPanel to wrap DynamicRenderer with ThingProvider
- [x] 5. Update ThingView to wrap recursive rendering with ThingProvider
- [x] 6. Update EmbedApp to wrap renderer with ThingProvider
- [x] 7. Test with demo renderer in main view (multiple panels)
- [x] 8. Test with embed view
- [x] 9. Update LOCAL_CODE_OVERVIEW.md with new hook documentation
- [x] 10. Update CODE_OVERVIEW.md implementation status
