/**
 * Persistent State Store
 *
 * In-memory store for renderer state that persists across component re-mounts.
 * State is keyed by thing shortId + user-provided key.
 * Multiple views of the same thing share state via pub/sub reactivity.
 */

type Listener = () => void;

class PersistentStateStore {
  private state = new Map<string, unknown>();
  private listeners = new Map<string, Set<Listener>>();

  /**
   * Get state value for a key.
   */
  get<T>(key: string): T | undefined {
    return this.state.get(key) as T | undefined;
  }

  /**
   * Set state value for a key.
   * Accepts either a direct value or an updater function (prev => next).
   * Setting undefined removes the key from storage (optimization).
   */
  set<T>(key: string, valueOrUpdater: T | undefined | ((prev: T | undefined) => T | undefined)): void {
    const newValue = typeof valueOrUpdater === 'function'
      ? (valueOrUpdater as (prev: T | undefined) => T | undefined)(this.state.get(key) as T | undefined)
      : valueOrUpdater;

    if (newValue === undefined) {
      this.state.delete(key);
    } else {
      this.state.set(key, newValue);
    }
    // Notify all listeners for this key
    this.listeners.get(key)?.forEach((fn) => fn());
  }

  /**
   * Subscribe to changes for a specific key.
   * Returns unsubscribe function.
   */
  subscribe(key: string, listener: Listener): () => void {
    if (!this.listeners.has(key)) {
      this.listeners.set(key, new Set());
    }
    this.listeners.get(key)!.add(listener);

    return () => {
      this.listeners.get(key)?.delete(listener);
      // Clean up empty listener sets
      if (this.listeners.get(key)?.size === 0) {
        this.listeners.delete(key);
      }
    };
  }

  /**
   * Get a snapshot function for useSyncExternalStore.
   */
  getSnapshot<T>(key: string): () => T | undefined {
    return () => this.get<T>(key);
  }
}

/**
 * Global singleton store instance.
 */
export const persistentStateStore = new PersistentStateStore();
