/**
 * useActiveMarks - Registry-based system for tracking "active" things
 *
 * Uses the Registry pattern (see docs/registry_pattern.md) for declarative
 * registration of active marks. Each mark includes source info for debugging.
 *
 * Sources of marks:
 * - 'snapshot': Things captured at the current tick
 * - 'selection': The currently selected thing (in DetailsPanel)
 * - 'panel': Things with open ThingViewPanels
 * - 'pin': Pinned things
 *
 * A thing is "active" if it has at least one mark from any source.
 */

import React, { createContext, useContext, useRef, useMemo, useCallback } from 'react';
import { Registry, useRegister, useRegistrySnapshot } from '../lib/Registry';

// =============================================================================
// Types
// =============================================================================

export type MarkSource = 'snapshot' | 'selection' | 'panel' | 'pin' | 'breadcrumb';

export interface ActiveMark {
  /** The thing key (b64) */
  key: string;
  /** Where this mark came from */
  source: MarkSource;
  /** Optional debug label (e.g., panel ID) */
  label?: string;
}

// =============================================================================
// Constants
// =============================================================================

/** Registry key for all active marks */
const ACTIVE_MARKS_KEY = 'activeMarks';

// =============================================================================
// Context
// =============================================================================

const ActiveMarksContext = createContext<Registry | null>(null);

// =============================================================================
// Provider
// =============================================================================

interface ActiveMarksProviderProps {
  children: React.ReactNode;
}

/**
 * Provider for the active marks system.
 *
 * Creates a single Registry instance that persists for the lifetime of the provider.
 * Components use useRegister or <Registration> to add marks, and useActiveKeys/useActiveMarks
 * to subscribe to the current set of active things.
 */
// Set to true to enable debug logging for active marks
const DEBUG_ACTIVE_MARKS = false;

export function ActiveMarksProvider({ children }: ActiveMarksProviderProps) {
  // Create registry once, never replace it
  const registryRef = useRef<Registry | null>(null);
  if (!registryRef.current) {
    registryRef.current = new Registry({ debugName: 'activeMarks', debug: DEBUG_ACTIVE_MARKS });
  }

  return (
    <ActiveMarksContext.Provider value={registryRef.current}>
      {children}
    </ActiveMarksContext.Provider>
  );
}

// =============================================================================
// Hooks
// =============================================================================

/**
 * Get the active marks registry.
 * Used for passing to useRegister or <Registration>.
 */
export function useActiveMarksRegistry(): Registry {
  const registry = useContext(ActiveMarksContext);
  if (!registry) {
    throw new Error('useActiveMarksRegistry must be used within an ActiveMarksProvider');
  }
  return registry;
}

/**
 * Register an active mark for a thing.
 *
 * This is a convenience wrapper around useRegister that creates a memoized
 * ActiveMark value. Pass null for key to skip registration.
 *
 * @param key - The thing key (b64), or null to skip registration
 * @param source - The source of this mark
 * @param label - Optional debug label
 */
export function useRegisterActiveMark(
  key: string | null,
  source: MarkSource,
  label?: string
): void {
  const registry = useContext(ActiveMarksContext);

  // Memoize the mark value to avoid re-registrations
  const mark = useMemo((): ActiveMark | null => {
    if (key === null) return null;
    return { key, source, label };
  }, [key, source, label]);

  useRegister(registry, ACTIVE_MARKS_KEY, mark);
}

/** Stability of an active thing */
export type ThingStability = 'stable' | 'transient';

/**
 * Get the map of all active thing keys with their stability.
 *
 * A key is "stable" if it has at least one mark with:
 * - source='snapshot'
 * - source='pin'
 * - source='panel' (thing-specific panels)
 *
 * Otherwise it's "transient" (only selection or breadcrumb marks).
 *
 * This is the primary hook for consumers that need to know which things
 * are active and whether they're stable or transient.
 */
export function useActiveKeys(): Map<string, ThingStability> {
  const registry = useContext(ActiveMarksContext);
  const marks = useRegistrySnapshot<ActiveMark>(registry, ACTIVE_MARKS_KEY);

  return useMemo(() => {
    const map = new Map<string, ThingStability>();
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

/**
 * Get all active marks (for debugging).
 *
 * Returns the full list of marks with source info.
 */
export function useActiveMarks(): ActiveMark[] {
  const registry = useContext(ActiveMarksContext);
  return useRegistrySnapshot<ActiveMark>(registry, ACTIVE_MARKS_KEY);
}

/**
 * Check if a specific key is active.
 *
 * Returns a function that checks if a key is in the active map.
 * The function is stable and only changes when the active keys change.
 */
export function useIsActive(): (key: string) => boolean {
  const activeKeys = useActiveKeys();
  return useCallback((key: string) => activeKeys.has(key), [activeKeys]);
}

/**
 * Get the stability of a specific key.
 *
 * Returns a function that returns the stability of a key, or undefined if not active.
 */
export function useGetStability(): (key: string) => ThingStability | undefined {
  const activeKeys = useActiveKeys();
  return useCallback((key: string) => activeKeys.get(key), [activeKeys]);
}
