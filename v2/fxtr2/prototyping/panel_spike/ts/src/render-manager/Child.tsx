/**
 * Child component - used by entity renderers to display owned children.
 *
 * Uses the useSlotContent hook which combines slot registration with subscription.
 * The hook registers the slot when subscribing, and unregisters on cleanup.
 */

import React, { useId, useState, useCallback } from 'react';
import { OutPortal } from 'react-reverse-portal';
import { useViewRegistry, useCurrentViewId, useSlotContent, useEntityData } from './contexts';
import type { EntityId, ViewId, SlotId, ViewRegistry } from './ViewRegistry';
import { isDragForEntity, parseDragData } from './ViewRegistry';
import { ViewContextMenu } from './ViewContextMenu';
import { useRegisteredEntityRenderer } from './registry';
import type { EntityType } from '../data/schema';
import { EntityPreviewCard } from './HoverPreview';

// =============================================================================
// Placeholder Component
// =============================================================================

interface PlaceholderProps {
  entityId: EntityId;
  viewId: ViewId;
  slotId: SlotId;
  viewRegistry: ViewRegistry;
}

/**
 * Shown when an entity is already rendered elsewhere.
 * Clicking jumps to the best existing view.
 * Accepts drops of views with matching entityId.
 */
function Placeholder({ entityId, viewId, slotId, viewRegistry }: PlaceholderProps) {
  const [isDragOver, setIsDragOver] = useState(false);
  const [isAltHeld, setIsAltHeld] = useState(false);
  const entity = useEntityData(entityId);
  const entityType = entity?.type as EntityType | undefined;

  // Get the inline renderer for the entity type
  const InlineRenderer = useRegisteredEntityRenderer(
    entityType ?? 'rubric',
    'inline'
  );

  const handleClick = () => {
    viewRegistry.jumpToEntity(entityId);
  };

  // Check if the drag is for this specific entity (works during dragover)
  const isMatchingDrag = useCallback(
    (e: React.DragEvent): boolean => {
      return isDragForEntity(e.dataTransfer, entityId);
    },
    [entityId]
  );

  const handleDragOver = useCallback(
    (e: React.DragEvent) => {
      if (isMatchingDrag(e)) {
        e.preventDefault(); // Allow drop
        e.dataTransfer.dropEffect = e.altKey ? 'copy' : 'move';
        setIsAltHeld(e.altKey);
      }
    },
    [isMatchingDrag]
  );

  const handleDragEnter = useCallback(
    (e: React.DragEvent) => {
      if (isMatchingDrag(e)) {
        e.preventDefault();
        setIsDragOver(true);
        setIsAltHeld(e.altKey);
        // Tell ViewRegistry a placeholder is active (prevents dockview from accepting)
        viewRegistry.setPlaceholderDragActive(true);
      }
    },
    [isMatchingDrag, viewRegistry]
  );

  const handleDragLeave = useCallback(() => {
    setIsDragOver(false);
    setIsAltHeld(false);
    viewRegistry.setPlaceholderDragActive(false);
  }, [viewRegistry]);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      setIsAltHeld(false);
      // Clear the flag regardless of whether we handle the drop
      viewRegistry.setPlaceholderDragActive(false);

      const dragData = parseDragData(e.dataTransfer);
      if (dragData?.type !== 'view' || dragData.entityId !== entityId) return;

      if (e.altKey) {
        // Alt+drop: duplicate into this slot
        viewRegistry.duplicateViewInSlot(dragData.viewId, slotId);
      } else {
        // Normal drop: move view to this slot
        viewRegistry.moveViewToSlot(dragData.viewId, slotId);
      }
    },
    [entityId, slotId, viewRegistry]
  );

  // Dynamic styles based on drag state
  const borderColor = isDragOver
    ? isAltHeld
      ? '#7ee787' // Green for copy/duplicate
      : '#58a6ff' // Blue for move
    : '#666';
  const borderStyle = isDragOver ? 'solid' : 'dashed';
  const borderWidth = isDragOver ? 2 : 1;

  return (
    <EntityPreviewCard entityId={entityId}>
      <ViewContextMenu context={{ type: 'placeholder', entityId, slotId }}>
        <div
          onClick={handleClick}
          onDragOver={handleDragOver}
          onDragEnter={handleDragEnter}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          style={{
            padding: '8px 8px',
            background: isDragOver ? '#4a4a4a' : '#3a3a3a',
            border: `${borderWidth}px ${borderStyle} ${borderColor}`,
            borderRadius: 4,
            cursor: 'pointer',
            color: '#888',
            transition: 'all 0.15s ease',
          }}
        >
          <span style={{ display: 'contents', pointerEvents: 'none' }}>
            <InlineRenderer entityId={entityId} isOwned={false} />
            <span style={{ marginLeft: 8, color: '#888' }}>open elsewhere</span>
            <span style={{ marginLeft: 8, color: '#58a6ff', fontSize: '0.8em' }}>
              {isDragOver
                ? isAltHeld
                  ? '(drop to duplicate here)'
                  : '(drop to move here)'
                : '(click to jump)'}
            </span>
          </span>
        </div>
      </ViewContextMenu>
    </EntityPreviewCard>
  );
}

// =============================================================================
// Child Component
// =============================================================================

export interface ChildProps {
  entityId: EntityId;
  /** Optional custom arguments forwarded to the entity renderer */
  customArgs?: Record<string, unknown>;
}

/**
 * Component used by entity renderers to display owned children.
 *
 * Uses the useSlotContent hook for slot registration and subscription.
 * CustomArgs are forwarded via OutPortal props to EntityRenderer.
 */
export function Child({ entityId, customArgs }: ChildProps) {
  const viewRegistry = useViewRegistry();
  const slotId = useId();
  const parentViewId = useCurrentViewId();

  // Use the cached slot content hook
  const content = useSlotContent(viewRegistry, slotId, entityId, parentViewId);

  if (content.type === 'pending') {
    return (
      <div style={{ color: '#888', fontStyle: 'italic' }}>
        [Pending slot {slotId}]
      </div>
    );
  }

  if (content.type === 'placeholder') {
    return (
      <Placeholder
        entityId={entityId}
        viewId={content.viewId}
        slotId={slotId}
        viewRegistry={viewRegistry}
      />
    );
  }

  // content.type === 'view'
  // div is possibly necessary to prevent React from erroring while using insertBefore:
  // https://github.com/httptoolkit/react-reverse-portal/issues/22
  // OutPortal forwards props to the component in InPortal (EntityRenderer)
  return (
    <ViewContextMenu context={{ type: 'child-owning', entityId, viewId: content.viewId }}>
      <div><OutPortal node={content.portalNode} customArgs={customArgs} /></div>
    </ViewContextMenu>
  );
}
