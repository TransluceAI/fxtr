/**
 * EntityTab - Custom tab header component for entity panels.
 *
 * Shows a PinOff icon for preview panels and handles double-click to pin.
 * Includes a close button on the right.
 */

import React, { useCallback } from 'react';
import type { IDockviewPanelHeaderProps } from 'dockview';
import { PinOff, X } from 'lucide-react';
import { useViewRegistry } from '../render-manager/contexts';
import type { ViewId, PanelId } from '../render-manager/ViewRegistry';

export interface EntityTabParams {
  viewId: ViewId;
  isPreview: boolean;
}

/**
 * Custom tab component for entity panels.
 * Shows PinOff icon for preview panels, double-click to pin, close button on right.
 */
export function EntityTab(props: IDockviewPanelHeaderProps<EntityTabParams>) {
  const { api, params } = props;
  const { isPreview } = params;
  const viewRegistry = useViewRegistry();
  const panelId = api.id as PanelId;

  const handleDoubleClick = useCallback(() => {
    if (isPreview) {
      viewRegistry.upgradePreviewToRegular(panelId);
    }
  }, [viewRegistry, panelId, isPreview]);

  const handleClose = useCallback((e: React.MouseEvent) => {
    e.stopPropagation(); // Don't trigger tab selection
    api.close();
  }, [api]);

  return (
    <div
      onDoubleClick={handleDoubleClick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        padding: '0 4px 0 8px',
        height: '100%',
        cursor: 'pointer',
        fontStyle: isPreview ? 'italic' : 'normal',
      }}
    >
      {isPreview && (
        <PinOff size={12} style={{ color: '#888', flexShrink: 0 }} />
      )}
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {api.title}
      </span>
      <button
        onClick={handleClose}
        style={{
          background: 'transparent',
          border: 'none',
          padding: 2,
          marginLeft: 4,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: 2,
          flexShrink: 0,
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = '#444'; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
        title="Close tab"
      >
        <X size={12} style={{ color: '#888' }} />
      </button>
    </div>
  );
}
