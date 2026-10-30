# Registry Pattern for Child-to-Parent State Propagation

This document describes a pattern for cases where child components need to register state that a distant ancestor needs to react to, without causing render loops.

**Implementation**: See `thingwatcher/frontend/lib/Registry.tsx` for the actual implementation used in this project.

## Problem

Child components want to "mount" state that requires an ancestor component to update its reactive state. The naive approach causes render loops:

1. Child mounts, calls `registerX()` in useEffect
2. `registerX()` updates parent's React state (e.g., `setVersion(v => v + 1)`)
3. Parent re-renders, recreating context value
4. Child's useEffect re-runs (because context changed in deps)
5. Cleanup calls `unregisterX()`, which updates state again
6. Loop!

## Solution: Registry with useSyncExternalStore

Create an imperative registry object that:
- Stores registered values outside React's state system
- Notifies subscribers via `useSyncExternalStore` (which batches updates correctly)
- Is created once and never replaced, so it's a stable reference

### Registry Implementation

```typescript
type Listener = () => void;

/**
 * An imperative registry that stores values by key, with subscription support
 * for useSyncExternalStore.
 *
 * Uses immutable updates: register/unregister create new arrays rather than
 * mutating in place. This allows getSnapshot to return the internal array
 * directly, since the reference only changes when contents change.
 *
 * The class is not generic - it stores unknown[] internally and uses generic
 * methods for type safety at call sites. This allows a single registry instance
 * to store different value types under different keys.
 *
 * IMPORTANT: Values passed to register() should be memoized by the caller
 * to avoid unnecessary effect re-runs.
 */
class Registry {
  private data = new Map<string, unknown[]>();
  private listeners = new Map<string, Set<Listener>>();

  // Cached empty array to avoid recreating on every getSnapshot call for empty keys
  private static readonly EMPTY: readonly never[] = Object.freeze([]);

  /**
   * Register a value under a key.
   * Creates a new array (immutable update) so getSnapshot returns a new reference.
   */
  register<V>(key: string, value: V): void {
    const list = this.data.get(key) ?? [];
    this.data.set(key, [...list, value]);
    this.notify(key);
  }

  /**
   * Unregister a value from a key.
   * Uses reference equality to find the value.
   * Creates a new array (immutable update) so getSnapshot returns a new reference.
   */
  unregister<V>(key: string, value: V): void {
    const list = this.data.get(key);
    if (!list) return;

    const idx = list.indexOf(value);
    if (idx >= 0) {
      const newList = list.filter((_, i) => i !== idx);

      // Clean up empty keys
      if (newList.length === 0) {
        this.data.delete(key);
      } else {
        this.data.set(key, newList);
      }

      this.notify(key);
    }
  }

  /**
   * Subscribe to changes for a key.
   * Returns an unsubscribe function.
   */
  subscribe(key: string, listener: Listener): () => void {
    let listeners = this.listeners.get(key);
    if (!listeners) {
      listeners = new Set();
      this.listeners.set(key, listeners);
    }
    listeners.add(listener);

    return () => {
      listeners!.delete(listener);
      if (listeners!.size === 0) {
        this.listeners.delete(key);
      }
    };
  }

  /**
   * Get a snapshot of values for a key.
   * Returns the internal array directly - safe because we use immutable updates.
   * Reference only changes when contents change, which is what useSyncExternalStore needs.
   */
  getSnapshot<V>(key: string): V[] {
    return (this.data.get(key) ?? Registry.EMPTY) as V[];
  }

  private notify(key: string): void {
    this.listeners.get(key)?.forEach(listener => listener());
  }
}
```

### Hook: useRegister

Used by child components to register their state:

```typescript
import { useEffect } from 'react';

/**
 * Register a value with the registry on mount, unregister on unmount.
 *
 * IMPORTANT: The `value` should be memoized (via useMemo) to avoid
 * unnecessary effect re-runs. If the value reference changes, the
 * effect will unregister the old value and register the new one.
 *
 * Pass null for value to skip registration (useful for conditional registration).
 */
function useRegister<V>(
  registry: Registry | null,
  key: string,
  value: V | null
): void {
  useEffect(() => {
    if (!registry || value === null) return;

    registry.register(key, value);
    return () => registry.unregister(key, value);
  }, [registry, key, value]);
}
```

### Hook: useRegistrySnapshot

Used by consumer components to subscribe to registered values:

```typescript
import { useSyncExternalStore, useCallback } from 'react';

/**
 * Subscribe to all values registered under a key.
 * Returns a stable array reference that only changes when contents change.
 */
function useRegistrySnapshot<V>(
  registry: Registry | null,
  key: string
): V[] {
  const subscribe = useCallback(
    (listener: () => void) => registry?.subscribe(key, listener) ?? (() => {}),
    [registry, key]
  );

  const getSnapshot = useCallback(
    () => registry?.getSnapshot<V>(key) ?? [],
    [registry, key]
  );

  return useSyncExternalStore(subscribe, getSnapshot);
}
```

### Provider Setup

The registry is created once and provided via context. Create a small context module for your use case:

```typescript
// MyFeatureRegistry.tsx
import { createContext, useContext, useRef, type ReactNode } from 'react';
import { Registry } from '../lib/Registry';

const MyRegistryContext = createContext<Registry | null>(null);

export function MyRegistryProvider({ children }: { children: ReactNode }) {
  // Create registry once, never replace it
  const registryRef = useRef<Registry | null>(null);
  if (!registryRef.current) {
    registryRef.current = new Registry();
  }

  return (
    <MyRegistryContext.Provider value={registryRef.current}>
      {children}
    </MyRegistryContext.Provider>
  );
}

export function useMyRegistry(): Registry | null {
  return useContext(MyRegistryContext);
}

// Optional: export key constants for type safety
export const ITEMS_KEY = 'items';
export const CONFIGS_KEY = 'configs';
```

## Usage Example

```typescript
import { useMemo } from 'react';
import { useRegister, useRegistrySnapshot } from '../lib/Registry';
import { useMyRegistry, ITEMS_KEY } from './MyFeatureRegistry';

// Child component registers itself
function ItemComponent({ id, data }: ItemProps) {
  const registry = useMyRegistry();

  // Memoize the value to avoid unnecessary re-registrations
  const itemInfo = useMemo(() => ({ id, data }), [id, data]);

  useRegister(registry, ITEMS_KEY, itemInfo);

  return <div className="item" />;
}

// Consumer component subscribes to all registered values
function ContainerComponent() {
  const registry = useMyRegistry();
  const allItems = useRegistrySnapshot<ItemInfo>(registry, ITEMS_KEY);

  // allItems updates when any item registers/unregisters
  return (
    <div>
      {allItems.length} items registered
    </div>
  );
}
```

## Why This Avoids the Render Loop

1. **Registry is stable**: Created once in a ref, never replaced
2. **Context value is stable**: The registry reference never changes
3. **Registration doesn't trigger React state**: It mutates the registry imperatively
4. **Only consumers re-render**: `useSyncExternalStore` triggers re-renders only in components that subscribe
5. **Child effects don't re-run**: Since context/registry is stable, useRegister's deps don't change

## Declarative Registration with `<Registration>`

For dynamic sets of values, use the `<Registration>` component instead of the hook. This is cleaner when you need to register multiple values based on an array:

```typescript
import { Registration } from '../lib/Registry';
import { useMyRegistry, ITEMS_KEY } from './MyFeatureRegistry';

// Register a dynamic set by mapping over the array
function DynamicItemsRegistrar({ items }: { items: Item[] }) {
  const registry = useMyRegistry();

  // Each item gets its own Registration component
  // When the items array changes, registrations are automatically updated
  return (
    <>
      {items.map(item => (
        <Registration
          key={item.id}  // React key for reconciliation
          registry={registry}
          registryKey={ITEMS_KEY}
          value={item}  // Should be stable (from the array)
        />
      ))}
    </>
  );
}
```

Benefits of `<Registration>` over `useRegister` for dynamic sets:
- No need for complex useEffect with cleanup tracking
- React's reconciliation handles mount/unmount automatically
- Cleaner code when the set of values changes over time

## Guidelines

1. **Memoize values**: Always memoize the value passed to `useRegister` to avoid unnecessary unregister/register cycles.

2. **Use broad keys when appropriate**: If all consumers need all values anyway (like edge configs for graph edges), use a single key like `'edgeConfigs'` rather than per-node keys.

3. **Handle null registry**: Components might render outside the provider (e.g., in tests). Check for null and no-op gracefully.

4. **Clean up empty keys**: The registry should remove empty lists to avoid memory leaks.

5. **Use `<Registration>` for dynamic sets**: When registering values from an array that changes over time, the component approach is cleaner than tracking registrations in a useEffect.
