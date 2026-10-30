/**
 * RenderRoot - Subscribes to the ViewRegistry and renders InPortals + Dockview.
 *
 * This is the main component that connects the ViewRegistry to React rendering.
 * It renders:
 * - InPortals for all active views (in a hidden container)
 * - DockviewReact (which contains the panels with OutPortals)
 */

import React, { useCallback, useRef, useState } from 'react';
import { DockviewReact, DockviewReadyEvent, DockviewDidDropEvent, DockviewApi, positionToDirection } from 'dockview';
import { InPortal } from 'react-reverse-portal';

import type { DataBundle } from '../data/ownership';
import type { ViewRegistry, PanelPosition } from './ViewRegistry';
import { getDragType, parseDragData } from './ViewRegistry';
import { DataBundleContext, useViews, useGroupLayout } from './contexts';
import { RendererRegistryContext } from './registry';
import { defaultRegistry } from '../renderers';
import { EntityRenderer } from './EntityRenderer';
import { DebugPanel } from '../panels/DebugPanel';
import { EntrypointPanel } from '../panels/EntrypointPanel';
import { EntityPanel } from '../panels/EntityPanel';
import { EntityTab } from '../panels/EntityTab';
import { GroupSidebar } from './GroupSidebar';
import { ResizableSidebar } from './ResizableSidebar';
import { collection_simple } from '../data/example-data';

export interface RenderRootProps {
  data: DataBundle;
  viewRegistry: ViewRegistry;
}

/**
 * RenderRoot renders InPortals for all views and sets up Dockview.
 */
export function RenderRoot({ data, viewRegistry }: RenderRootProps) {
  // Subscribe to views using cached snapshot
  const views = useViews(viewRegistry);

  // Subscribe to group layout for sidebar
  const groupLayout = useGroupLayout(viewRegistry);

  // Sidebar collapsed state
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  // Store dockview API ref for panel click handling
  const dockviewApiRef = useRef<DockviewApi | null>(null);

  // Handle panel click in sidebar
  const handlePanelClick = useCallback((panelId: string) => {
    const panel = dockviewApiRef.current?.getPanel(panelId);
    panel?.focus();
  }, []);

  // Handle panel close from sidebar
  const handlePanelClose = useCallback((panelId: string) => {
    const panel = dockviewApiRef.current?.getPanel(panelId);
    if (panel) {
      dockviewApiRef.current?.removePanel(panel);
    }
  }, []);

  // Handle panel double-click in sidebar (pins preview panels)
  const handlePanelDoubleClick = useCallback((panelId: string) => {
    viewRegistry.upgradePreviewToRegular(panelId);
  }, [viewRegistry]);

  // Handle sidebar collapse toggle
  const handleToggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => !prev);
  }, []);

  // Handle dockview ready
  const onDockviewReady = useCallback(
    (event: DockviewReadyEvent) => {
      viewRegistry.setDockviewApi(event.api);
      dockviewApiRef.current = event.api;

      // Set up panel close handler
      event.api.onDidRemovePanel((panel) => {
        viewRegistry.handlePanelClose(panel.id);
      });

      // Upgrade preview panels to regular when moved (via internal dockview drag)
      event.api.onDidMovePanel?.((e) => {
        viewRegistry.upgradePreviewToRegular(e.panel.id);
      });

      // Set up external DND handler - accept our custom drag format
      event.api.onUnhandledDragOverEvent((e) => {
        const dragType = getDragType(e.nativeEvent.dataTransfer);
        if (!dragType) return;

        // Don't accept if a placeholder is the intended drop target
        if (dragType === 'view' && viewRegistry.isPlaceholderDragActive()) {
          return; // Placeholder handles this drop
        }

        // Accept both 'view' and 'ref' drags (refs create new views)
        e.accept();
      });

      // Create initial panels - debug, entrypoint, and simple collection in same group
      event.api.addPanel({
        id: 'debug',
        component: 'debug',
        title: 'Debug',
        params: { data, viewRegistry },
      });
      event.api.addPanel({
        id: 'entrypoint',
        component: 'entrypoint',
        title: 'Collections',
        params: { data, viewRegistry },
        position: { referencePanel: 'debug', direction: 'within' },
      });
      // Open simple collection entity view as the active panel
      viewRegistry.openEntity(collection_simple.id);
    },
    [viewRegistry, data]
  );

  // Handle external drop onto dockview
  const onDidDrop = useCallback(
    (event: DockviewDidDropEvent) => {
      const dragData = parseDragData(event.nativeEvent.dataTransfer);
      if (!dragData) return;

      // Build position from drop event
      const dir = positionToDirection(event.position);
      const position: PanelPosition = {
        referenceGroup: event.group ?? undefined,
        direction: dir === 'within' ? undefined : dir,
      };

      if (dragData.type === 'ref') {
        // Refs: move existing view or create new (alt always creates new)
        viewRegistry.handleRefDrop(dragData.entityId, position, event.nativeEvent.altKey);
        return;
      }

      if (event.nativeEvent.altKey) {
        // Alt+drop duplicates the view with children
        viewRegistry.duplicateViewInPanel(dragData.viewId, position);
      } else {
        // Normal drop moves the view to a new panel
        viewRegistry.moveViewToNewPanel(dragData.viewId, position);
      }
    },
    [viewRegistry]
  );

  return (
    <DataBundleContext.Provider value={data}>
      {/*
        NOTE: The renderer registry is currently hardcoded. Eventually these will
        be dynamically fetched/loaded when entity types are first encountered.
      */}
      <RendererRegistryContext.Provider value={defaultRegistry}>
        {/* Hidden InPortal container */}
        <div style={{ display: 'none' }}>
          {views.map((view) => (
            // IMPORTANT: EntityRenderer must be the direct child of InPortal.
            // Props passed to OutPortal (like customArgs) are forwarded to this
            // direct child. EntityRenderer sets up CurrentViewContext internally.
            <InPortal key={view.viewId} node={view.portalNode}>
              <EntityRenderer
                viewId={view.viewId}
                entityId={view.entityId}
                isTopLevel={view.panelId !== null}
              />
            </InPortal>
          ))}
        </div>

        {/* Main layout with sidebar and dockview */}
        <div style={{ display: 'flex', height: '100%' }}>
          <ResizableSidebar defaultWidth={220} minWidth={150} maxWidth={400} collapsed={sidebarCollapsed}>
            <GroupSidebar
              layout={groupLayout}
              onPanelClick={handlePanelClick}
              onPanelClose={handlePanelClose}
              onPanelDoubleClick={handlePanelDoubleClick}
              collapsed={sidebarCollapsed}
              onToggleCollapsed={handleToggleSidebar}
            />
          </ResizableSidebar>
          <div style={{ flex: 1, minWidth: 0 }}>
            <DockviewReact
              className="dockview-theme-abyss"
              onReady={onDockviewReady}
              onDidDrop={onDidDrop}
              components={{
                debug: DebugPanel,
                entrypoint: EntrypointPanel,
                entity: EntityPanel,
              }}
              tabComponents={{
                entity: EntityTab,
              }}
            />
          </div>
        </div>
      </RendererRegistryContext.Provider>
    </DataBundleContext.Provider>
  );
}

// Keep the old name as an alias for backwards compatibility during transition
export const RenderManager = RenderRoot;
