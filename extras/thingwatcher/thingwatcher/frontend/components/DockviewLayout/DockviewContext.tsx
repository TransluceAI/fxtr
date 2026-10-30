/**
 * Dockview context and utilities - extracted to avoid circular dependencies.
 * This file can be imported by ThingContextMenu and ThingWatcherBindings
 * without creating circular imports with DockviewLayout.
 */
import { createContext, useContext } from 'react';
import type { DockviewApi, DockviewGroupPanel } from 'dockview';

// Context for dockview API access
const DockviewApiContext = createContext<DockviewApi | null>(null);

export { DockviewApiContext };

/**
 * Hook to access dockview API from any component within the dockview tree.
 * Returns null if not within a dockview context.
 */
export function useDockviewApiFromContext(): DockviewApi | null {
  return useContext(DockviewApiContext);
}

/**
 * Helper function to open a thing in a new panel.
 * Panel ID uses the full key to ensure uniqueness.
 * If a panel already exists for this key, focuses it instead of creating a new one.
 * Used by context menu "Open in new panel" action.
 */
export function openThingPanel(api: DockviewApi, key: string) {
  const panelId = `thing-view-${key}`;

  // Check if panel already exists
  const existingPanel = api.getPanel(panelId);
  if (existingPanel) {
    existingPanel.api.setActive();
    return;
  }

  // Add new panel
  api.addPanel({
    id: panelId,
    component: 'thingView',
    title: 'Thing View',
    params: { key },
  });
}

/** Position for creating a new panel via drag-drop */
export interface ThingPanelPosition {
  referenceGroup?: DockviewGroupPanel;
  direction?: 'left' | 'right' | 'above' | 'below';
}

/**
 * Helper function to open a thing in a new panel at a specific position.
 * Always creates a new panel, even if one already exists for this key.
 * Used by drag-drop onto dockview.
 */
export function openThingPanelWithPosition(
  api: DockviewApi,
  key: string,
  position?: ThingPanelPosition
) {
  // Generate unique panel ID with timestamp to allow duplicates
  const panelId = `thing-view-${key}-${Date.now()}`;

  api.addPanel({
    id: panelId,
    component: 'thingView',
    title: 'Thing View',
    params: { key },
    position: position?.referenceGroup
      ? {
          referenceGroup: position.referenceGroup,
          direction: position.direction,
        }
      : undefined,
  });
}
