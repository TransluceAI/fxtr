/**
 * Registry - A generic subscription-based registry for child-to-parent state propagation.
 *
 * This module provides a pattern for cases where child components need to register
 * state that a distant ancestor needs to react to, without causing render loops.
 *
 * See docs/registry_pattern.md for detailed documentation.
 *
 * Key features:
 * - Stores values by string key
 * - Uses immutable updates (new array on each change) for useSyncExternalStore compatibility
 * - Provides useRegister hook for registration and useRegistrySnapshot for subscription
 */

import { useEffect, useCallback, useSyncExternalStore } from 'react';

type Listener = () => void;

// Cached empty array to avoid recreating on every getSnapshot call for empty keys
const EMPTY: readonly never[] = Object.freeze([]);

export interface RegistryOptions {
  /** Name for debug logging */
  debugName?: string;
  /** Enable debug logging for this registry */
  debug?: boolean;
}

/**
 * An imperative registry that stores values by key, with subscription support
 * for useSyncExternalStore.
 *
 * Uses immutable updates: register/unregister create new arrays rather than
 * mutating in place. This allows getSnapshot to return the internal array
 * directly, since the reference only changes when contents change.
 *
 * The class stores unknown[] internally and uses generic methods for type safety
 * at call sites. This allows a single registry instance to store different value
 * types under different keys.
 */
export class Registry {
  private data = new Map<string, unknown[]>();
  private listeners = new Map<string, Set<Listener>>();
  private debugName?: string;
  private debug: boolean;

  constructor(options?: RegistryOptions) {
    this.debugName = options?.debugName;
    this.debug = options?.debug ?? false;
  }

  /**
   * Register a value under a key.
   * Creates a new array (immutable update) so getSnapshot returns a new reference.
   */
  register<V>(key: string, value: V): void {
    const list = this.data.get(key) ?? [];
    this.data.set(key, [...list, value]);
    if (this.debug) {
      console.log(`[Registry${this.debugName ? `:${this.debugName}` : ''}] register`, key, value, `-> new list is`, this.data.get(key));
    }
    this.notify(key);
  }

  /**
   * Unregister a value from a key.
   * Uses reference equality to find the value.
   * Creates a new array (immutable update) so getSnapshot returns a new reference.
   */
  unregister<V>(key: string, value: V): void {
    const list = this.data.get(key);
    if (!list) {
      if (this.debug) {
        console.warn(`[Registry${this.debugName ? `:${this.debugName}` : ''}] unregister (no list)`, key, value);
      }
      return;
    }

    const idx = list.indexOf(value);
    if (idx >= 0) {
      const newList = list.filter((_, i) => i !== idx);

      // Clean up empty keys
      if (newList.length === 0) {
        this.data.delete(key);
      } else {
        this.data.set(key, newList);
      }

      if (this.debug) {
        console.log(`[Registry${this.debugName ? `:${this.debugName}` : ''}] unregister`, key, value, `-> new list is`, this.data.get(key));
      }
      this.notify(key);
    } else if (this.debug) {
      console.log(`[Registry${this.debugName ? `:${this.debugName}` : ''}] unregister (not found)`, key, value);
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
    return (this.data.get(key) ?? EMPTY) as V[];
  }

  private notify(key: string): void {
    this.listeners.get(key)?.forEach((listener) => listener());
  }
}

/**
 * Register a value with a registry on mount, unregister on unmount.
 *
 * IMPORTANT: The `value` should be memoized (via useMemo) to avoid
 * unnecessary effect re-runs. If the value reference changes, the
 * effect will unregister the old value and register the new one.
 *
 * Pass null for value to skip registration (useful for conditional registration).
 */
export function useRegister<V>(registry: Registry | null, key: string, value: V | null): void {
  useEffect(() => {
    if (!registry || value === null) return;

    registry.register(key, value);
    return () => registry.unregister(key, value);
  }, [registry, key, value]);
}

/**
 * Subscribe to all values registered under a key.
 * Returns a stable array that only changes when contents change.
 */
export function useRegistrySnapshot<V>(registry: Registry | null, key: string): V[] {
  const subscribe = useCallback(
    (listener: () => void) => registry?.subscribe(key, listener) ?? (() => {}),
    [registry, key]
  );

  const getSnapshot = useCallback(() => registry?.getSnapshot<V>(key) ?? [], [registry, key]);

  return useSyncExternalStore(subscribe, getSnapshot);
}

// =============================================================================
// Declarative Registration Component
// =============================================================================

export interface RegistrationProps<V> {
  /** The registry to register with */
  registry: Registry | null;
  /** The key to register under */
  registryKey: string;
  /** The value to register (should be memoized or stable) */
  value: V;
}

/**
 * Declarative component for registering a value with a registry.
 *
 * Renders nothing - just registers the value on mount and unregisters on unmount.
 * Useful for registering a dynamic set of values by mapping over them:
 *
 * ```tsx
 * {things.map(t => (
 *   <Registration
 *     key={t.key}
 *     registry={registry}
 *     registryKey="activeThings"
 *     value={markValue}
 *   />
 * ))}
 * ```
 *
 * IMPORTANT: The `value` prop should be memoized or stable to avoid
 * unnecessary re-registrations.
 */
export function Registration<V>({ registry, registryKey, value }: RegistrationProps<V>): null {
  useRegister(registry, registryKey, value);
  return null;
}
