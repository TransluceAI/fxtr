/**
 * DraggableID - Draggable entity ID badge for entity renderings.
 *
 * Displays the entity ID and allows dragging to:
 * - Dockview panels (creates new panel or moves view)
 * - Placeholder slots (moves view to that slot)
 *
 * When used inside a view (has viewId from context):
 * - Drag moves the view, Alt+drag duplicates
 *
 * When used outside a view (e.g., in preview cards):
 * - Drag creates a new view (acts like RefLink)
 *
 * Hovering shows a preview card with "Contained in" and "Referenced by" lists.
 * Shows backlinks count badge when there are backlinks.
 */

import React, { useCallback } from 'react';
import { MessageSquareReply } from 'lucide-react';
import type { EntityId } from './ViewRegistry';
import { serializeDragData } from './ViewRegistry';
import { useDataBundle, useViewContext } from './contexts';
import { getReferencedBy } from '../data/ownership';
import { BacklinksPreviewCard } from './HoverPreview';
import { RefLink } from './EntityRenderer';

export interface DraggableIDProps {
  entityId: EntityId;
}

export function DraggableID({ entityId }: DraggableIDProps) {
  const data = useDataBundle();
  const ctx = useViewContext();
  const viewId = ctx?.viewId ?? null;
  const inHoverPreview = ctx?.inHoverPreview ?? false;
  const backlinks = getReferencedBy(data, entityId);
  const hasBacklinks = backlinks.length > 0;

  // Can only move if we have a viewId AND we're not in a hover preview
  // In hover previews, we still have the viewId (for source panel lookup) but can't move
  const canMove = viewId !== null && !inHoverPreview;

  const handleDragStart = useCallback(
    (e: React.DragEvent) => {
      if (canMove && viewId) {
        // In view context and not in hover: drag data includes view info for move/duplicate
        serializeDragData({ type: 'view', viewId, entityId }, e.dataTransfer);
      } else {
        // In hover preview or no view context: drag creates new view (like RefLink)
        serializeDragData({ type: 'ref', entityId }, e.dataTransfer);
      }
      // Also set text/plain for compatibility
      e.dataTransfer.setData('text/plain', entityId);
      e.dataTransfer.effectAllowed = 'copyMove';
    },
    [entityId, viewId, canMove]
  );

  const handleDrag = useCallback((e: React.DragEvent) => {
    // Update drop effect based on alt key (only relevant when can move)
    if (e.altKey) {
      e.dataTransfer.dropEffect = 'copy';
    } else {
      e.dataTransfer.dropEffect = 'move';
    }
  }, []);

  const title = canMove
    ? 'Drag to move view, Alt+drag to duplicate'
    : 'Drag to open in new panel';

  return (
    <BacklinksPreviewCard
      entityId={entityId}
      renderBacklink={(refId) => <RefLink entityId={refId} direction="left" />}
    >
      <span
        draggable
        onDragStart={handleDragStart}
        onDrag={handleDrag}
        style={{
          cursor: 'grab',
          userSelect: 'none',
          padding: '2px 4px',
          borderRadius: 3,
          background: 'rgba(255, 255, 255, 0.1)',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
        }}
        title={title}
      >
        {entityId}
        {hasBacklinks && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 2, color: '#888' }}>
            <MessageSquareReply size={12} />
            <span style={{ fontSize: '0.85em' }}>{backlinks.length}</span>
          </span>
        )}
      </span>
    </BacklinksPreviewCard>
  );
}
