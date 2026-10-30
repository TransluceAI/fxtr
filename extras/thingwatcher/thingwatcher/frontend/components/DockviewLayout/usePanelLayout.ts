/**
 * Hook for tracking dockview panel layout.
 * Subscribes to dockview events and returns current layout structure.
 */

import { useSyncExternalStore, useCallback, useRef } from 'react';
import type { DockviewApi } from 'dockview';

// Types for sidebar layout
export interface GroupLayout {
  groupId: string;
  panels: PanelLayout[];
}

export interface PanelLayout {
  panelId: string;
  title: string;
  isActive: boolean;
  thingKey?: string; // For thing panels only (b64 key)
}

// Build layout structure from dockview API
function buildLayout(api: DockviewApi): GroupLayout[] {
  const groups: GroupLayout[] = [];

  for (const group of api.groups) {
    const panels: PanelLayout[] = [];

    for (const panel of group.panels) {
      // Use panel.api.component to determine panel type
      // component is 'thingView', 'graph', 'details', 'thingsList', 'roots'
      const isThingPanel = panel.api.component === 'thingView';
      const thingKey = isThingPanel ? (panel.params?.key as string | undefined) : undefined;

      panels.push({
        panelId: panel.id,
        title: panel.title ?? panel.id,
        isActive: api.activePanel?.id === panel.id,
        thingKey,
      });
    }

    groups.push({ groupId: group.id, panels });
  }

  return groups;
}

/**
 * Hook to track dockview panel layout.
 * Returns current layout structure, updates when panels/groups change.
 */
export function usePanelLayout(api: DockviewApi | null): GroupLayout[] {
  // Cache the layout - only rebuild when version changes
  const layoutRef = useRef<GroupLayout[]>([]);
  const versionRef = useRef(0);
  const lastVersionRef = useRef(-1);

  // Subscribe function for useSyncExternalStore
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (!api) return () => {};

      // Build initial layout
      layoutRef.current = buildLayout(api);
      lastVersionRef.current = versionRef.current;

      // Helper to rebuild and notify
      const rebuildAndNotify = () => {
        versionRef.current++;
        layoutRef.current = buildLayout(api);
        lastVersionRef.current = versionRef.current;
        onStoreChange();
      };

      // Subscribe to all relevant dockview events
      const disposables = [
        api.onDidAddPanel(rebuildAndNotify),
        api.onDidRemovePanel(rebuildAndNotify),
        api.onDidAddGroup(rebuildAndNotify),
        api.onDidRemoveGroup(rebuildAndNotify),
        api.onDidActivePanelChange(rebuildAndNotify),
        // onDidLayoutChange fires when panels are moved between groups, resized, etc.
        api.onDidLayoutChange(rebuildAndNotify),
      ];

      return () => {
        disposables.forEach((d) => d.dispose());
      };
    },
    [api]
  );

  // Snapshot function - returns cached layout (same reference unless rebuilt by subscribe)
  const getSnapshot = useCallback(() => {
    // If api changed and we haven't subscribed yet, build initial layout
    if (api && lastVersionRef.current === -1) {
      layoutRef.current = buildLayout(api);
      lastVersionRef.current = versionRef.current;
    }
    return layoutRef.current;
  }, [api]);

  // Server snapshot - same as client for now
  const getServerSnapshot = useCallback(() => {
    return [];
  }, []);

  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
