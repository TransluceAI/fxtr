/**
 * HoverContext - Context providers and hooks for hover preview cards
 *
 * Provides two contexts:
 * - HoverKeptOpenContext: For nested hovers to keep parent hovers open
 * - HoverCloseContext: For closing all hovers when navigation happens
 */

import { createContext, useContext } from 'react';

// =============================================================================
// HoverKeptOpenContext
// =============================================================================

/**
 * Context for nested hover cards. When a child hover opens, it calls the
 * parent's setKeptOpen(true) to prevent the parent from closing.
 *
 * Usage: A hover card provides this context to its children. Child hover cards
 * use useSetKeptOpen() to notify the parent when they open/close.
 */
export const HoverKeptOpenContext = createContext<((open: boolean) => void) | null>(null);

/**
 * Hook to get the parent's setKeptOpen callback for nested hovers.
 * Returns null if not inside a hover card.
 */
export function useSetKeptOpen(): ((open: boolean) => void) | null {
  return useContext(HoverKeptOpenContext);
}

// =============================================================================
// HoverCloseContext
// =============================================================================

/**
 * Context for closing all hovers when navigation happens.
 * Provides a callback that any component can call to close all open hovers.
 *
 * The provider (DockviewLayout or EmbedApp) manages the list of close callbacks
 * registered by hover cards.
 */
export interface HoverCloseContextValue {
  /** Register a callback to be called when hovers should close. Returns unsubscribe function. */
  registerCloseListener: (callback: () => void) => () => void;
  /** Close all registered hovers */
  closeAllHovers: () => void;
}

export const HoverCloseContext = createContext<HoverCloseContextValue | null>(null);

/**
 * Hook to get the hover close context.
 * Returns null if not inside a HoverCloseContext provider.
 */
export function useHoverClose(): HoverCloseContextValue | null {
  return useContext(HoverCloseContext);
}

/**
 * Hook to get just the closeAllHovers function.
 * Returns a no-op function if not inside a HoverCloseContext provider.
 */
export function useCloseAllHovers(): () => void {
  const ctx = useContext(HoverCloseContext);
  return ctx?.closeAllHovers ?? (() => {});
}
