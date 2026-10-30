/**
 * ThingUIEventsContext - Internal context for UI event handlers
 *
 * Provides UI event handlers that thing-related components (NodeRef, ThingContextMenu)
 * need to hook into the broader application. Different apps provide different implementations:
 *
 * - App (main): onClickThing selects, onOpenThingInNewPanel opens dockview panel
 * - EmbedApp: onClickThing copies to clipboard, onOpenThingInNewPanel opens new browser window
 */

import React, { createContext, useContext } from 'react';

// =============================================================================
// Types
// =============================================================================

export interface ThingUIEventsContextValue {
  /** Called when clicking a thing reference (NodeRef) */
  onClickThing: (key: string) => void;
  /** Called for "open in new panel/window" action (context menu) */
  onOpenThingInNewPanel: (key: string) => void;
  /** Label for the open action: "panel" (App) or "window" (EmbedApp) */
  openInNewPanelLabel: 'panel' | 'window';
}

// =============================================================================
// Context
// =============================================================================

const ThingUIEventsContext = createContext<ThingUIEventsContextValue | null>(null);

// =============================================================================
// Provider
// =============================================================================

export interface ThingUIEventsProviderProps {
  children: React.ReactNode;
  value: ThingUIEventsContextValue;
}

export function ThingUIEventsProvider({ children, value }: ThingUIEventsProviderProps) {
  return (
    <ThingUIEventsContext.Provider value={value}>
      {children}
    </ThingUIEventsContext.Provider>
  );
}

// =============================================================================
// Hook
// =============================================================================

/**
 * Access the ThingUIEvents context.
 * Must be used within a ThingUIEventsProvider.
 *
 * @throws Error if not within a ThingUIEventsProvider
 */
export function useThingUIEvents(): ThingUIEventsContextValue {
  const ctx = useContext(ThingUIEventsContext);
  if (!ctx) {
    throw new Error('useThingUIEvents must be used within a ThingUIEventsProvider');
  }
  return ctx;
}
