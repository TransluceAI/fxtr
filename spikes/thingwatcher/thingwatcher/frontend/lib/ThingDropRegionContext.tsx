/**
 * ThingDropRegionContext - Context for managing ThingDropRegion suppression of dockview drops
 *
 * When a ThingDropRegion is being dragged over, it signals to dockview that it should
 * not accept the drop. This allows custom renderers to define their own drop zones
 * that take priority over dockview's panel creation.
 *
 * Architecture:
 * - ThingDropRegion components call setDropRegionActive(true) on dragenter, false on dragleave/drop
 * - DockviewLayout checks isDropRegionActive and skips e.accept() if true
 * - Counter-based tracking handles nested elements (dragenter/dragleave fire for each child)
 */

import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';

export interface ThingDropRegionContextValue {
  /** Whether any ThingDropRegion is currently being dragged over */
  isDropRegionActive: boolean;
  /** Increment the active counter (called on dragenter) */
  incrementDropRegion: () => void;
  /** Decrement the active counter (called on dragleave/drop) */
  decrementDropRegion: () => void;
}

const ThingDropRegionContext = createContext<ThingDropRegionContextValue | null>(null);

/**
 * Hook to access ThingDropRegion context.
 * Returns null if not in a provider (e.g., EmbedApp may not have dockview to suppress).
 */
export function useThingDropRegionContext(): ThingDropRegionContextValue | null {
  return useContext(ThingDropRegionContext);
}

/**
 * Provider component for ThingDropRegion context.
 * Should be placed above DockviewLayout in the component tree.
 */
export function ThingDropRegionProvider({ children }: { children: React.ReactNode }) {
  // Counter-based tracking for nested elements
  // When counter > 0, a drop region is active
  const [counter, setCounter] = useState(0);

  const incrementDropRegion = useCallback(() => {
    setCounter((c) => c + 1);
  }, []);

  const decrementDropRegion = useCallback(() => {
    setCounter((c) => Math.max(0, c - 1));
  }, []);

  const value = useMemo(
    (): ThingDropRegionContextValue => ({
      isDropRegionActive: counter > 0,
      incrementDropRegion,
      decrementDropRegion,
    }),
    [counter, incrementDropRegion, decrementDropRegion]
  );

  return (
    <ThingDropRegionContext.Provider value={value}>
      {children}
    </ThingDropRegionContext.Provider>
  );
}
