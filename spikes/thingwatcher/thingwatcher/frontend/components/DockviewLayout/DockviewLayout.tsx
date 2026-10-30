import React, { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import {
  DockviewReact,
  DockviewReadyEvent,
  DockviewDidDropEvent,
  DockviewApi,
  SerializedDockview,
  positionToDirection,
} from 'dockview';
import { DockviewApiContext, useDockviewApiFromContext, openThingPanel, openThingPanelWithPosition } from './DockviewContext';
import { usePanelLayout } from './usePanelLayout';
import { DRAG_MIME_THING, parseThingDragData } from '../../lib/dragConstants';
import { usePinnedThings } from '../../hooks';
import { GraphPanelWrapper } from './GraphPanelWrapper';
import { DetailsPanelWrapper } from './DetailsPanelWrapper';
import { ThingsListPanelWrapper } from './ThingsListPanelWrapper';
import { RootsPanelWrapper } from './RootsPanelWrapper';
import { ThingViewPanel } from './ThingViewPanel';
import { ResizableSidebar, PanelSidebar } from '../Sidebar';
// ThingContextMenu is now a wrapper component - used directly by DetailsPanel, ThingsList, NodeRef
import { ThingUIEventsProvider, type ThingUIEventsContextValue } from '../../lib/ThingUIEventsContext';
import { HoverCloseContext, type HoverCloseContextValue } from '../../lib/HoverContext';
import { ThingDropRegionProvider, useThingDropRegionContext } from '../../lib/ThingDropRegionContext';
import type { Thing, ChangeInfo, TriggerRecord, Root } from '../../types';
import styles from './DockviewLayout.module.css';

const LAYOUT_STORAGE_KEY = 'thingwatcher-dockview-layout';

// Main panel IDs that should be auto-reopened if closed
const MAIN_PANEL_IDS = ['graph-main', 'details-main', 'thingslist-main', 'roots-main'];

// Panel component registry
const components = {
  graph: GraphPanelWrapper,
  details: DetailsPanelWrapper,
  thingsList: ThingsListPanelWrapper,
  roots: RootsPanelWrapper,
  thingView: ThingViewPanel,
};

export interface DockviewLayoutProps {
  // Sidebar props
  sidebarVisible: boolean;
  // Graph props
  things: Thing[];
  changeInfo: ChangeInfo;
  selectedThingKey: string | null;  // b64 key - canonical identifier
  onSelectThing: (key: string | null) => void;
  // DetailsPanel props
  selectedThing: Thing | null;
  detailsLoading: boolean;
  isLive: boolean;
  triggerRecords: TriggerRecord[];
  onTrigger: () => void;
  // ThingsList props
  triggeredShortIds: Set<number>;
  // RootsPanel props
  roots: Root[];
  // Current tick for ThingViewPanel queries
  currentTickIndex: number;
}

// Context for dockview props (used by panel wrapper components)
const DockviewPropsContext = createContext<DockviewLayoutProps | null>(null);

/**
 * Hook to access dockview props from panel components.
 */
export function useDockviewProps(): DockviewLayoutProps {
  const ctx = useContext(DockviewPropsContext);
  if (!ctx) {
    throw new Error('useDockviewProps must be used within DockviewLayout');
  }
  return ctx;
}

// Re-export from DockviewContext for convenience
export { useDockviewApiFromContext, openThingPanel };

// Helper to add a main panel back
function addMainPanel(api: DockviewApi, panelId: string) {
  switch (panelId) {
    case 'graph-main':
      api.addPanel({
        id: 'graph-main',
        component: 'graph',
        title: 'Graph',
      });
      break;
    case 'details-main': {
      const graphPanel = api.getPanel('graph-main');
      api.addPanel({
        id: 'details-main',
        component: 'details',
        title: 'Selected Thing',
        position: graphPanel
          ? { referencePanel: graphPanel, direction: 'right' }
          : undefined,
      });
      break;
    }
    case 'thingslist-main': {
      const detailsPanel = api.getPanel('details-main');
      api.addPanel({
        id: 'thingslist-main',
        component: 'thingsList',
        title: 'All Things',
        position: detailsPanel
          ? { referencePanel: detailsPanel, direction: 'below' }
          : undefined,
      });
      break;
    }
    case 'roots-main': {
      const thingsListPanel = api.getPanel('thingslist-main');
      api.addPanel({
        id: 'roots-main',
        component: 'roots',
        title: 'Roots',
        position: thingsListPanel
          ? { referencePanel: thingsListPanel, direction: 'below' }
          : undefined,
      });
      break;
    }
  }
}

export function DockviewLayout(props: DockviewLayoutProps) {
  const apiRef = useRef<DockviewApi | null>(null);
  const [apiState, setApiState] = useState<DockviewApi | null>(null);
  const { addBreadcrumbs } = usePinnedThings();

  // ThingDropRegion context - when active, dockview should not accept drops
  const dropRegionContext = useThingDropRegionContext();
  // Use ref to access latest value from within event handlers
  const isDropRegionActiveRef = useRef(false);
  isDropRegionActiveRef.current = dropRegionContext?.isDropRegionActive ?? false;

  // Panel layout for sidebar
  const panelLayout = usePanelLayout(apiState);

  // Sidebar handlers
  const handlePanelFocus = useCallback((panelId: string) => {
    apiState?.getPanel(panelId)?.api.setActive();
  }, [apiState]);

  const handlePanelClose = useCallback((panelId: string) => {
    const panel = apiState?.getPanel(panelId);
    if (panel) {
      apiState?.removePanel(panel);
    }
  }, [apiState]);

  // Hover close state - stores callbacks from hover cards that should be called to close them
  const hoverCloseListenersRef = useRef<Set<() => void>>(new Set());

  const hoverCloseContextValue = useMemo((): HoverCloseContextValue => ({
    registerCloseListener: (callback: () => void) => {
      hoverCloseListenersRef.current.add(callback);
      return () => {
        hoverCloseListenersRef.current.delete(callback);
      };
    },
    closeAllHovers: () => {
      hoverCloseListenersRef.current.forEach((cb) => cb());
    },
  }), []);

  // Build ThingUIEvents context value
  // Note: NodeRef/ChildNodeView use hooks directly for queries and rendering
  const handleOpenThingInNewPanel = useCallback((key: string) => {
    if (apiState) {
      openThingPanel(apiState, key);
    }
  }, [apiState]);

  // Wrap onSelectThing to also close hovers
  const handleClickThing = useCallback((key: string | null) => {
    hoverCloseContextValue.closeAllHovers();
    props.onSelectThing(key);
  }, [hoverCloseContextValue, props.onSelectThing]);

  const thingUIEventsContextValue = useMemo((): ThingUIEventsContextValue => ({
    onClickThing: handleClickThing,
    onOpenThingInNewPanel: handleOpenThingInNewPanel,
    openInNewPanelLabel: 'panel',
  }), [handleClickThing, handleOpenThingInNewPanel]);

  // Create default layout
  const createDefaultLayout = useCallback((api: DockviewApi) => {
    // Create graph panel on the left
    const graphPanel = api.addPanel({
      id: 'graph-main',
      component: 'graph',
      title: 'Graph',
    });

    // Create details panel on the right top
    const detailsPanel = api.addPanel({
      id: 'details-main',
      component: 'details',
      title: 'Selected Thing',
      position: { referencePanel: graphPanel, direction: 'right' },
    });

    // Create things list panel on the right middle
    const thingsListPanel = api.addPanel({
      id: 'thingslist-main',
      component: 'thingsList',
      title: 'All Things',
      position: { referencePanel: detailsPanel, direction: 'below' },
    });

    // Create roots panel on the right bottom
    api.addPanel({
      id: 'roots-main',
      component: 'roots',
      title: 'Roots',
      position: { referencePanel: thingsListPanel, direction: 'below' },
    });
  }, []);

  // Handle dockview ready
  const onReady = useCallback((event: DockviewReadyEvent) => {
    const api = event.api;
    apiRef.current = api;
    setApiState(api);

    // Try to restore saved layout
    const savedLayout = localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (savedLayout) {
      try {
        const layout = JSON.parse(savedLayout) as SerializedDockview;
        api.fromJSON(layout);

        // Check if main panels exist, add any that are missing
        for (const panelId of MAIN_PANEL_IDS) {
          if (!api.getPanel(panelId)) {
            console.warn(`Main panel ${panelId} missing from saved layout, adding it`);
            addMainPanel(api, panelId);
          }
        }
      } catch (e) {
        console.error('Failed to restore layout:', e);
        createDefaultLayout(api);
      }
    } else {
      createDefaultLayout(api);
    }

    // Listen for panel removal - reopen main panels if they're closed
    const removeDisposable = api.onDidRemovePanel((panel) => {
      if (MAIN_PANEL_IDS.includes(panel.id)) {
        console.log(`Main panel ${panel.id} was closed, reopening...`);
        // Use setTimeout to avoid modifying during the removal event
        setTimeout(() => {
          addMainPanel(api, panel.id);
        }, 0);
      }
    });

    // Save layout on changes
    const layoutDisposable = api.onDidLayoutChange(() => {
      const layout = api.toJSON();
      localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layout));
    });

    // Accept external drag-drop of things onto dockview
    // Skip accepting if a ThingDropRegion is currently being dragged over
    const dragOverDisposable = api.onUnhandledDragOverEvent((e) => {
      // Don't accept if a drop region wants this drop
      if (isDropRegionActiveRef.current) {
        return;
      }
      const types = Array.from(e.nativeEvent.dataTransfer?.types ?? []);
      if (types.includes(DRAG_MIME_THING)) {
        e.accept();
      }
    });

    return () => {
      removeDisposable.dispose();
      layoutDisposable.dispose();
      dragOverDisposable.dispose();
    };
  }, [createDefaultLayout]);

  // Handle external drop onto dockview - creates new panel at drop position
  const onDidDrop = useCallback((event: DockviewDidDropEvent) => {
    if (!event.nativeEvent.dataTransfer || !apiRef.current) return;

    const dropInfo = parseThingDragData(event.nativeEvent.dataTransfer);
    if (!dropInfo) return;

    // Create all pending breadcrumbs from the drag
    if (dropInfo.pendingBreadcrumbs && dropInfo.pendingBreadcrumbs.length > 0) {
      addBreadcrumbs(dropInfo.pendingBreadcrumbs);
    }

    const direction = positionToDirection(event.position);
    openThingPanelWithPosition(apiRef.current, dropInfo.key, {
      referenceGroup: event.group ?? undefined,
      direction: direction === 'within' ? undefined : direction,
    });
  }, [addBreadcrumbs]);

  return (
    <DockviewApiContext.Provider value={apiState}>
      <DockviewPropsContext.Provider value={props}>
        <HoverCloseContext.Provider value={hoverCloseContextValue}>
          <ThingUIEventsProvider value={thingUIEventsContextValue}>
            <div className={`${styles.container} dockview-theme-dark`}>
              {props.sidebarVisible && (
                <ResizableSidebar>
                  <PanelSidebar
                    layout={panelLayout}
                    onPanelFocus={handlePanelFocus}
                    onPanelClose={handlePanelClose}
                    selectedThingKey={props.selectedThingKey}
                  />
                </ResizableSidebar>
              )}
              <DockviewReact
                className={styles.dockview}
                components={components}
                onReady={onReady}
                onDidDrop={onDidDrop}
              />
            </div>
          </ThingUIEventsProvider>
        </HoverCloseContext.Provider>
      </DockviewPropsContext.Provider>
    </DockviewApiContext.Provider>
  );
}
