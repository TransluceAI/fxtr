/**
 * EntityRenderer - Renders the content of an entity using the renderer registry.
 *
 * This component wraps the type-specific full renderer with highlight handling
 * and view ID tracking. The actual rendering logic is delegated to registered
 * renderers in src/renderers/by_entity_type/.
 */

import React from 'react';
import type { EntityId, EntityType } from '../data/schema';
import { serializeDragData } from './ViewRegistry';
import { useViewRegistry, CurrentViewContext, useIsHighlighted, useEntityData, useViewContext } from './contexts';
import { useRegisteredEntityRenderer } from './registry';
import { ViewContextMenu } from './ViewContextMenu';
import { EntityPreviewCard } from './HoverPreview';

// =============================================================================
// EntityRenderer
// =============================================================================

export interface EntityRendererProps {
  entityId: EntityId;
  /** The view ID for this renderer instance */
  viewId: string;
  /** True if this is a top-level panel view, false if nested in a parent entity */
  isTopLevel: boolean;
  /** Optional custom arguments for renderer customization */
  customArgs?: Record<string, unknown>;
}

/**
 * Renders an entity using the registered full renderer.
 * Handles highlight state and provides CurrentViewContext for children.
 *
 * Note: This component sets up CurrentViewContext internally because it must be
 * the direct child of InPortal for OutPortal prop forwarding to work.
 */
export function EntityRenderer({ entityId, viewId, isTopLevel, customArgs }: EntityRendererProps) {
  const isHighlighted = useIsHighlighted(viewId);
  const entity = useEntityData(entityId);

  if (!entity) {
    return <div style={{ color: '#f88' }}>Entity not found: {entityId}</div>;
  }

  // Get the registered full renderer for this entity type
  const FullRenderer = useRegisteredEntityRenderer(entity.type as EntityType, 'full');

  return (
    <CurrentViewContext.Provider value={{ viewId, inHoverPreview: false }}>
      <div
        data-view-id={viewId}
        style={{
          boxShadow: isHighlighted ? '0 0 12px 4px white' : undefined,
          transition: 'box-shadow 0.3s ease-out',
        }}
      >
        <FullRenderer
          entityId={entityId}
          isTopLevel={isTopLevel}
          customArgs={customArgs}
        />
      </div>
    </CurrentViewContext.Provider>
  );
}

// =============================================================================
// RefLink Component - Block reference (expands to fill, shows content preview)
// =============================================================================

/** Direction for opening panels relative to source */
export type OpenDirection = 'left' | 'right';

export interface RefLinkProps {
  entityId: EntityId;
  /** True if rendering an owned child, false if an unowned reference */
  isOwned?: boolean;
  /** Direction to open panel relative to source. Defaults to 'right' (forward navigation). */
  direction?: OpenDirection;
}

/**
 * Renders a clickable and draggable block reference link to an entity.
 * Uses the registered ref renderer for the entity's type.
 * Block refs expand to fill available space and show content preview.
 * Clicking opens the entity in a panel or jumps to existing view.
 * Dragging to a panel creates a new view there.
 */
export function RefLink({ entityId, isOwned = false, direction = 'right' }: RefLinkProps) {
  const viewRegistry = useViewRegistry();
  const entity = useEntityData(entityId);
  const entityType = entity?.type as EntityType | undefined;
  const ctx = useViewContext();

  // Compute source panel from context's viewId (works even in hover previews)
  const sourcePanelId = ctx?.viewId ? viewRegistry.findViewPanel(ctx.viewId) : null;

  // Get the registered ref renderer (fallback handled by hook)
  const RefRenderer = useRegisteredEntityRenderer(
    entityType ?? 'rubric', // fallback type if entity not found
    'ref'
  );

  const handleClick = () => {
    viewRegistry.openInPreviewOrJumpTo(entityId, direction, sourcePanelId);
  };

  const handleDoubleClick = () => {
    viewRegistry.openInPreviewOrJumpTo(entityId, direction, sourcePanelId, { upgradePreview: true });
  };

  const handleDragStart = (e: React.DragEvent) => {
    // Set our custom drag data with type 'ref'
    serializeDragData({ type: 'ref', entityId }, e.dataTransfer);
    // Also set text/plain for compatibility
    e.dataTransfer.setData('text/plain', entityId);
    e.dataTransfer.effectAllowed = 'copyMove';
  };

  return (
    <EntityPreviewCard entityId={entityId}>
      <ViewContextMenu context={{ type: 'ref', entityId }}>
        <div
          draggable
          onClick={handleClick}
          onDoubleClick={handleDoubleClick}
          onDragStart={handleDragStart}
          style={{
            cursor: 'pointer',
          }}
          title="Click to show. Double-click to pin. Drag to open in new panel"
        >
          <RefRenderer
            entityId={entityId}
            isOwned={isOwned}
          />
        </div>
      </ViewContextMenu>
    </EntityPreviewCard>
  );
}

// =============================================================================
// InlineRefLink Component - Inline reference (minimal, for inline contexts)
// =============================================================================

export interface InlineRefLinkProps {
  entityId: EntityId;
  /** True if rendering an owned child, false if an unowned reference */
  isOwned?: boolean;
  /** Direction to open panel relative to source. Defaults to 'right' (forward navigation). */
  direction?: OpenDirection;
}

/**
 * Renders a clickable and draggable inline reference link to an entity.
 * Uses the registered inline renderer for the entity's type.
 * Inline refs are minimal and flow with surrounding text.
 * Clicking opens the entity in a panel or jumps to existing view.
 * Dragging to a panel creates a new view there.
 */
export function InlineRefLink({ entityId, isOwned = false, direction = 'right' }: InlineRefLinkProps) {
  const viewRegistry = useViewRegistry();
  const entity = useEntityData(entityId);
  const entityType = entity?.type as EntityType | undefined;
  const ctx = useViewContext();

  // Compute source panel from context's viewId (works even in hover previews)
  const sourcePanelId = ctx?.viewId ? viewRegistry.findViewPanel(ctx.viewId) : null;

  // Get the registered inline renderer (fallback handled by hook)
  const InlineRenderer = useRegisteredEntityRenderer(
    entityType ?? 'rubric', // fallback type if entity not found
    'inline'
  );

  const handleClick = () => {
    viewRegistry.openInPreviewOrJumpTo(entityId, direction, sourcePanelId);
  };

  const handleDoubleClick = () => {
    viewRegistry.openInPreviewOrJumpTo(entityId, direction, sourcePanelId, { upgradePreview: true });
  };

  const handleDragStart = (e: React.DragEvent) => {
    // Set our custom drag data with type 'ref'
    serializeDragData({ type: 'ref', entityId }, e.dataTransfer);
    // Also set text/plain for compatibility
    e.dataTransfer.setData('text/plain', entityId);
    e.dataTransfer.effectAllowed = 'copyMove';
  };

  return (
    <EntityPreviewCard entityId={entityId}>
      <ViewContextMenu context={{ type: 'ref', entityId }}>
        <span
          draggable
          onClick={handleClick}
          onDoubleClick={handleDoubleClick}
          onDragStart={handleDragStart}
          style={{
            cursor: 'pointer',
          }}
          title="Click to show. Double-click to pin. Drag to open in new panel"
        >
          <InlineRenderer
            entityId={entityId}
            isOwned={isOwned}
          />
        </span>
      </ViewContextMenu>
    </EntityPreviewCard>
  );
}
