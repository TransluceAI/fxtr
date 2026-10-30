/**
 * EntityPanel - Dockview panel that displays an entity at the top level.
 *
 * This panel receives a viewId and renders an OutPortal connected to
 * the view's portalNode. Also displays parent entities ("Contained in:")
 * above the content and back-references ("Referenced by:") below.
 */

import React, { useSyncExternalStore } from 'react';
import type { IDockviewPanelProps } from 'dockview';
import { OutPortal } from 'react-reverse-portal';
import { useViewRegistry, useDataBundle, CurrentViewContext } from '../render-manager/contexts';
import type { ViewId } from '../render-manager/ViewRegistry';
import { ViewContextMenu } from '../render-manager/ViewContextMenu';
import { RefLink } from '../render-manager/EntityRenderer';
import { getOwners, getReferencedBy } from '../data/ownership';

export interface EntityPanelParams {
  viewId: ViewId;
}

/**
 * Panel component for displaying entity views in dockview.
 */
export function EntityPanel({ params, api }: IDockviewPanelProps<EntityPanelParams>) {
  const { viewId } = params;
  const panelId = api.id;
  const viewRegistry = useViewRegistry();
  const data = useDataBundle();

  // Subscribe to view (in case it changes, though unlikely)
  const view = useSyncExternalStore(
    viewRegistry.subscribe,
    () => viewRegistry.getView(viewId)
  );

  // Check if this panel is a preview panel
  const isPreview = viewRegistry.isPanelPreview(panelId);

  // Handle click to upgrade preview panel to regular
  const handleContentClick = () => {
    if (isPreview) {
      viewRegistry.upgradePreviewToRegular(panelId);
    }
  };

  if (!view) {
    return (
      <div style={{ padding: 16, color: '#f88' }}>
        View not found: {viewId}
      </div>
    );
  }

  const entityId = view.entityId;
  const parents = getOwners(data, entityId);
  const backRefs = getReferencedBy(data, entityId);

  return (
    <CurrentViewContext.Provider value={{ viewId, inHoverPreview: false }}>
      <div style={{ padding: 16, height: '100%', overflow: 'auto' }} onClick={handleContentClick}>
        {/* Parents (Contained in:) - navigate left (backward) */}
        {parents.length > 0 && (
          <div style={{ marginBottom: 12, color: '#888', fontSize: '0.9em' }}>
            <span>Contained in: </span>
            <ul>
              {parents.map((parentId) => (
                <li key={parentId}>
                  <RefLink entityId={parentId} direction="left" />
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Entity content */}
        <ViewContextMenu context={{ type: 'root', entityId: view.entityId, viewId }}>
          <div><OutPortal node={view.portalNode} /></div>
        </ViewContextMenu>

        {/* Back-references (Referenced by:) - navigate left (backward) */}
        {backRefs.length > 0 && (
          <div style={{ marginTop: 12, color: '#888', fontSize: '0.9em' }}>
            <span>Referenced by: </span>
            <ul>
            {backRefs.map((refId) => (
              <li key={refId}>
                <RefLink entityId={refId} direction="left" />
              </li>
            ))}
            </ul>
          </div>
        )}
      </div>
    </CurrentViewContext.Provider>
  );
}
