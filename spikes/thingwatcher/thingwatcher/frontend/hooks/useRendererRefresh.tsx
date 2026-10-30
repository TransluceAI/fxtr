/**
 * Renderer refresh context and hooks.
 *
 * Provides a mechanism for components to subscribe to "refresh renderers" events.
 * When the refresh button is clicked, all subscribed callbacks are invoked.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';

// =============================================================================
// Context
// =============================================================================

interface RendererRefreshContextValue {
  subscribe: (callback: () => void) => void;
  unsubscribe: (callback: () => void) => void;
  triggerRefresh: () => void;
}

const RendererRefreshContext = createContext<RendererRefreshContextValue | null>(null);

// =============================================================================
// Provider
// =============================================================================

interface RendererRefreshProviderProps {
  children: React.ReactNode;
}

/**
 * Provider that enables renderer refresh functionality.
 *
 * Wrap your app with this provider to enable the refresh button and
 * useOnRendererRefresh hook.
 */
export function RendererRefreshProvider({ children }: RendererRefreshProviderProps) {
  // Use ref to hold the Set of callbacks (stable across renders)
  const listenersRef = useRef(new Set<() => void>());

  const subscribe = useCallback((callback: () => void) => {
    listenersRef.current.add(callback);
  }, []);

  const unsubscribe = useCallback((callback: () => void) => {
    listenersRef.current.delete(callback);
  }, []);

  const triggerRefresh = useCallback(() => {
    for (const callback of listenersRef.current) {
      callback();
    }
  }, []);

  const value = useMemo(
    () => ({ subscribe, unsubscribe, triggerRefresh }),
    [subscribe, unsubscribe, triggerRefresh]
  );

  return (
    <RendererRefreshContext.Provider value={value}>
      {children}
    </RendererRefreshContext.Provider>
  );
}

// =============================================================================
// Hooks
// =============================================================================

/**
 * Hook to get the trigger function for the refresh button.
 *
 * Call the returned function to trigger a refresh of all renderer configs.
 *
 * @throws Error if used outside of RendererRefreshProvider
 */
export function useRendererRefreshTrigger(): () => void {
  const ctx = useContext(RendererRefreshContext);
  if (!ctx) {
    throw new Error('useRendererRefreshTrigger requires RendererRefreshProvider');
  }
  return ctx.triggerRefresh;
}

/**
 * Hook to register a callback that will be called when refresh is triggered.
 *
 * The callback is automatically subscribed on mount and unsubscribed on unmount.
 * If the callback reference changes, it will re-subscribe with the new callback.
 *
 * @param callback Function to call when refresh is triggered
 */
export function useOnRendererRefresh(callback: () => void): void {
  const ctx = useContext(RendererRefreshContext);

  useEffect(() => {
    if (!ctx) return;
    ctx.subscribe(callback);
    return () => ctx.unsubscribe(callback);
  }, [ctx, callback]);
}
