/**
 * PinnedThingsContext - Persistent marks for things the user has pinned + breadcrumbs
 *
 * Pinning a thing:
 * - Adds it to the pinned set
 * - Registers an active mark via the registry system
 * - Pinned things appear in a separate section at the top of ThingsList
 * - Pinned things stay loaded even if no panels are open for them
 *
 * Breadcrumbs:
 * - Track navigation history between things
 * - When user navigates from A to B, a breadcrumb A->B is created
 * - As long as B is active, A stays active via the breadcrumb
 * - Breadcrumbs cascade: if B becomes inactive, the A->B breadcrumb is removed
 * - Prevents cycles: no opposite-direction breadcrumbs (B->A when A->B exists)
 *
 * Both pin state and breadcrumbs are session-only (not persisted to localStorage).
 */

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useMemo,
  useRef,
  useEffect,
} from 'react';
import {
  useActiveMarksRegistry,
  useActiveKeys,
  useRegisterActiveMark,
  type ActiveMark,
} from './useActiveMarks';
import { Registry, Registration, useRegister, useRegistrySnapshot } from '../lib/Registry';
import type { BreadcrumbType, PendingBreadcrumb } from './useBreadcrumbSource';

// =============================================================================
// Types
// =============================================================================

/** A breadcrumb with unique ID (for removal) */
export interface Breadcrumb extends PendingBreadcrumb {
  /** Unique ID for this breadcrumb */
  id: string;
}

export interface PinnedThingsContextValue {
  // --- Pin API ---
  /** Set of b64 keys for all pinned things */
  pinnedKeys: Set<string>;
  /** Toggle pin state for a thing */
  togglePin: (key: string) => void;
  /** Pin a thing (no-op if already pinned) */
  pin: (key: string) => void;
  /** Unpin a thing (no-op if not pinned) */
  unpin: (key: string) => void;
  /** Check if a thing is pinned */
  isPinned: (key: string) => boolean;

  // --- Breadcrumb API ---
  /** Add breadcrumbs (from pending chain). Skips existing same/opposite direction. */
  addBreadcrumbs: (breadcrumbs: PendingBreadcrumb[]) => void;
  /** Remove a specific breadcrumb by ID */
  removeBreadcrumb: (id: string) => void;
  /** Registry for efficient breadcrumb queries by toKey */
  breadcrumbsRegistry: Registry;
}

// =============================================================================
// Constants
// =============================================================================

/** Registry key for breadcrumbs (keyed by toKey) */
const BREADCRUMBS_KEY = 'breadcrumbs';

// =============================================================================
// Context
// =============================================================================

const PinnedThingsContext = createContext<PinnedThingsContextValue | null>(null);

// =============================================================================
// BreadcrumbMark Component
// =============================================================================

interface BreadcrumbMarkProps {
  breadcrumb: Breadcrumb;
  onRemove: (id: string) => void;
  breadcrumbsRegistry: Registry;
}

/**
 * BreadcrumbMark - Handles registration and lifecycle for a single breadcrumb.
 *
 * - Registers an active mark for the fromKey (keeps source thing active)
 * - Registers itself in the breadcrumbs registry (for useBreadcrumbsFor lookup)
 * - Self-removes when toKey becomes inactive (cascade behavior)
 * - Uses "settled" state to avoid premature cleanup during initial mount batch
 */
function BreadcrumbMark({ breadcrumb, onRemove, breadcrumbsRegistry }: BreadcrumbMarkProps) {
  const [isSettled, setIsSettled] = useState(false);
  const activeKeys = useActiveKeys();
  const isToKeyActive = activeKeys.has(breadcrumb.toKey);

  // Register mark for fromKey (keeps the source thing active)
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

// =============================================================================
// Provider
// =============================================================================

interface PinnedThingsProviderProps {
  children: React.ReactNode;
}

/**
 * Provider for the pinned things and breadcrumbs system.
 *
 * Integrates with the active marks registry:
 * - Pinned things are registered as active marks with source='pin'
 * - Breadcrumbs register marks for their fromKey with source='breadcrumb'
 */
export function PinnedThingsProvider({ children }: PinnedThingsProviderProps) {
  const activeMarksRegistry = useActiveMarksRegistry();

  // Pinned keys state
  const [pinnedKeys, setPinnedKeys] = useState<Set<string>>(new Set());

  // Breadcrumbs state
  const [breadcrumbs, setBreadcrumbs] = useState<Breadcrumb[]>([]);

  // Create breadcrumbs registry once (for efficient lookup by toKey)
  const breadcrumbsRegistryRef = useRef<Registry | null>(null);
  if (!breadcrumbsRegistryRef.current) {
    breadcrumbsRegistryRef.current = new Registry({ debugName: 'breadcrumbs' });
  }

  // Memoize pin marks for Registration components
  const pinMarks = useMemo((): ActiveMark[] => {
    return Array.from(pinnedKeys).map(key => ({ key, source: 'pin' as const }));
  }, [pinnedKeys]);

  // --- Pin methods ---

  const pin = useCallback((key: string) => {
    setPinnedKeys((prev) => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }, []);

  const unpin = useCallback((key: string) => {
    setPinnedKeys((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  }, []);

  const togglePin = useCallback(
    (key: string) => {
      if (pinnedKeys.has(key)) {
        unpin(key);
      } else {
        pin(key);
      }
    },
    [pinnedKeys, pin, unpin]
  );

  const isPinned = useCallback(
    (key: string): boolean => pinnedKeys.has(key),
    [pinnedKeys]
  );

  // --- Breadcrumb methods ---

  const addBreadcrumbs = useCallback((pendingBreadcrumbs: PendingBreadcrumb[]) => {
    setBreadcrumbs(prev => {
      let next = prev;
      for (const bc of pendingBreadcrumbs) {
        // Skip if fromKey === toKey (self-reference)
        if (bc.fromKey === bc.toKey) {
          continue;
        }

        // Check for existing breadcrumb in same direction
        const hasSameDirection = next.some(
          existing => existing.fromKey === bc.fromKey && existing.toKey === bc.toKey
        );
        // Check for existing breadcrumb in opposite direction
        const hasOppositeDirection = next.some(
          existing => existing.fromKey === bc.toKey && existing.toKey === bc.fromKey
        );

        // Don't create if either exists (prevents cycles)
        if (hasSameDirection || hasOppositeDirection) {
          continue;
        }

        const id = crypto.randomUUID();
        // Only create new array if we're actually adding
        if (next === prev) {
          next = [...prev];
        }
        next.push({ ...bc, id });
      }
      return next;
    });
  }, []);

  const removeBreadcrumb = useCallback((id: string) => {
    setBreadcrumbs(prev => prev.filter(bc => bc.id !== id));
  }, []);

  // --- Context value ---

  const value = useMemo(
    (): PinnedThingsContextValue => ({
      // Pin API
      pinnedKeys,
      togglePin,
      pin,
      unpin,
      isPinned,
      // Breadcrumb API
      addBreadcrumbs,
      removeBreadcrumb,
      breadcrumbsRegistry: breadcrumbsRegistryRef.current!,
    }),
    [pinnedKeys, togglePin, pin, unpin, isPinned, addBreadcrumbs, removeBreadcrumb]
  );

  return (
    <PinnedThingsContext.Provider value={value}>
      {/* Register pinned things as active */}
      {pinMarks.map(mark => (
        <Registration
          key={mark.key}
          registry={activeMarksRegistry}
          registryKey="activeMarks"
          value={mark}
        />
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

// =============================================================================
// Hooks
// =============================================================================

/**
 * Access the pinned things and breadcrumbs context.
 * Must be used within a PinnedThingsProvider.
 */
export function usePinnedThings(): PinnedThingsContextValue {
  const ctx = useContext(PinnedThingsContext);
  if (!ctx) {
    throw new Error('usePinnedThings must be used within a PinnedThingsProvider');
  }
  return ctx;
}

/**
 * Get all breadcrumbs pointing TO a specific thing.
 *
 * Uses the breadcrumbs registry for efficient lookup.
 */
export function useBreadcrumbsFor(toKey: string): Breadcrumb[] {
  const { breadcrumbsRegistry } = usePinnedThings();
  return useRegistrySnapshot<Breadcrumb>(breadcrumbsRegistry, toKey);
}
