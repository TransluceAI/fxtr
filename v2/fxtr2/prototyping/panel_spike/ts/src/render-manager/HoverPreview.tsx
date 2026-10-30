/**
 * HoverPreview - Components for hover preview cards.
 *
 * EntityPreviewCard: Wraps a trigger element and shows an entity preview on hover.
 * BacklinksPreviewCard: Wraps a trigger element and shows backlinks list on hover.
 *
 * Both support nested hover cards - hovering items inside the popup keeps the parent open.
 */

import { useState, useEffect, type ReactNode } from 'react';
import { PreviewCard } from '@base-ui/react/preview-card';
import { Tooltip } from '@base-ui/react/tooltip';
import { useDataBundle, useSetKeptOpen, HoverKeptOpenContext, useEntityData, CurrentViewContext, useViewRegistry, useViewContext } from './contexts';
import { useRegisteredEntityRenderer } from './registry';
import { getReferencedBy, getOwners } from '../data/ownership';
import type { EntityId, EntityType } from '../data/schema';

// =============================================================================
// EntityPreviewCard
// =============================================================================

export interface EntityPreviewCardProps {
  /** The entity ID to show preview for */
  entityId: EntityId;
  /** The trigger element (e.g., RefLink content) */
  children: ReactNode;
}

/**
 * Wraps a trigger element and shows an entity preview card on hover.
 * Uses the registered preview renderer for the entity's type.
 * Supports nested hover cards via HoverKeptOpenContext.
 *
 * Preserves viewId from parent context but sets inHoverPreview: true.
 * This allows RefLinks to know the source panel while DraggableID knows
 * it can't move (only create new views).
 */
export function EntityPreviewCard({ entityId, children }: EntityPreviewCardProps) {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);
  // Tracks if user has hovered since the last panel action.
  // This prevents delayed open events from firing after a panel opens.
  const [touchedBeforePanelAction, setTouchedBeforePanelAction] = useState(false);
  const parentSetKeptOpen = useSetKeptOpen();
  const viewRegistry = useViewRegistry();

  // Preserve the viewId from parent context (for source panel lookup in nested RefLinks)
  const parentCtx = useViewContext();
  const preservedViewId = parentCtx?.viewId ?? null;

  const isOpen = touchedBeforePanelAction && (open || keptOpenByChild || keptOpenByHover);

  // Propagate open state to parent preview card
  useEffect(() => {
    parentSetKeptOpen?.(isOpen);
  }, [isOpen, parentSetKeptOpen]);

  // Register close callback so ViewRegistry can close this hover when panels open
  useEffect(() => {
    return viewRegistry.registerHoverCloseListener(() => {
      setTouchedBeforePanelAction(false);
    });
  }, [viewRegistry]);

  // Get entity type for renderer lookup
  const entity = useEntityData(entityId);
  const entityType = entity?.type as EntityType | undefined;

  // Get the preview renderer
  const PreviewRenderer = useRegisteredEntityRenderer(
    entityType ?? 'rubric',
    'preview'
  );

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger
        delay={500}
        closeDelay={300}
        onMouseEnter={() => setTouchedBeforePanelAction(true)}
      >
        {children}
      </PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner side="top" align="start" alignOffset={20}>
          <PreviewCard.Popup
            onMouseEnter={() => setKeptOpenByHover(true)}
            onMouseLeave={() => setKeptOpenByHover(false)}
            style={{
              background: '#1a1a1a',
              border: '2px solid #555',
              borderRadius: 4,
              padding: 8,
              maxWidth: 300,
              zIndex: 1000,
              boxShadow: '0 4px 16px rgba(0, 0, 0, 0.4)',
            }}
          >
            <CurrentViewContext.Provider value={{ viewId: preservedViewId, inHoverPreview: true }}>
              <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
                <PreviewRenderer entityId={entityId} />
              </HoverKeptOpenContext.Provider>
            </CurrentViewContext.Provider>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}

// =============================================================================
// BacklinksPreviewCard
// =============================================================================

export interface BacklinksPreviewCardProps {
  /** The entity ID to show relationships for */
  entityId: EntityId;
  /** The trigger element (e.g., DraggableID content) */
  children: ReactNode;
  /** Function to render each related entity. Receives the entity ID. */
  renderBacklink: (entityId: EntityId) => ReactNode;
}

/**
 * Wraps a trigger element and shows entity relationships on hover.
 * Shows both "Contained in" (parents) and "Referenced by" (backlinks).
 * Uses renderBacklink prop to render each related entity (typically RefLink).
 * Supports nested hover cards via HoverKeptOpenContext.
 *
 * Preserves viewId from parent context but sets inHoverPreview: true.
 * This allows RefLinks to know the source panel while DraggableID knows
 * it can't move (only create new views).
 */
export function BacklinksPreviewCard({ entityId, children, renderBacklink }: BacklinksPreviewCardProps) {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);
  // Tracks if user has hovered since the last panel action.
  // This prevents delayed open events from firing after a panel opens.
  const [touchedBeforePanelAction, setTouchedBeforePanelAction] = useState(false);
  const parentSetKeptOpen = useSetKeptOpen();
  const viewRegistry = useViewRegistry();
  const data = useDataBundle();

  // Preserve the viewId from parent context (for source panel lookup in nested RefLinks)
  const parentCtx = useViewContext();
  const preservedViewId = parentCtx?.viewId ?? null;

  const isOpen = touchedBeforePanelAction && (open || keptOpenByChild || keptOpenByHover);

  // Propagate open state to parent preview card
  useEffect(() => {
    parentSetKeptOpen?.(isOpen);
  }, [isOpen, parentSetKeptOpen]);

  // Register close callback so ViewRegistry can close this hover when panels open
  useEffect(() => {
    return viewRegistry.registerHoverCloseListener(() => {
      setTouchedBeforePanelAction(false);
    });
  }, [viewRegistry]);

  const parents = getOwners(data, entityId);
  const backlinks = getReferencedBy(data, entityId);

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger
        delay={500}
        onMouseEnter={() => setTouchedBeforePanelAction(true)}
      >
        {children}
      </PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner side="top" align="start" alignOffset={20}>
          <PreviewCard.Popup
            onMouseEnter={() => setKeptOpenByHover(true)}
            onMouseLeave={() => setKeptOpenByHover(false)}
            style={{
              background: '#1a1a1a',
              border: '2px solid #555',
              borderRadius: 4,
              padding: 8,
              maxWidth: 300,
              zIndex: 1000,
              boxShadow: '0 4px 16px rgba(0, 0, 0, 0.4)',
            }}
          >
            <CurrentViewContext.Provider value={{ viewId: preservedViewId, inHoverPreview: true }}>
              <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
                {/* Contained in (parents) */}
                <div style={{ color: '#888', fontSize: '0.9em', marginBottom: 4 }}>
                  Contained in:
                </div>
                {parents.length === 0 ? (
                  <div style={{ color: '#666', fontStyle: 'italic', marginBottom: 8 }}>
                    (none)
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8 }}>
                    {parents.map((parentId) => (
                      <div key={parentId}>{renderBacklink(parentId)}</div>
                    ))}
                  </div>
                )}

                {/* Referenced by (backlinks) */}
                <div style={{ color: '#888', fontSize: '0.9em', marginBottom: 4 }}>
                  Referenced by:
                </div>
                {backlinks.length === 0 ? (
                  <div style={{ color: '#666', fontStyle: 'italic' }}>
                    (none)
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    {backlinks.map((refId) => (
                      <div key={refId}>{renderBacklink(refId)}</div>
                    ))}
                  </div>
                )}
              </HoverKeptOpenContext.Provider>
            </CurrentViewContext.Provider>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}

// =============================================================================
// EntitySidebarTooltip
// =============================================================================

export interface EntitySidebarTooltipProps {
  /** The entity ID to show preview for */
  entityId: EntityId;
  /** The trigger element */
  children: ReactNode;
}

/**
 * Simple tooltip wrapper for sidebar entity items.
 * Uses Tooltip instead of PreviewCard for simpler behavior (no hoverable popup).
 * Should be used inside a Tooltip.Provider for grouped delay behavior.
 */
export function EntitySidebarTooltip({ entityId, children }: EntitySidebarTooltipProps) {
  // Get entity type for renderer lookup
  const entity = useEntityData(entityId);
  const entityType = entity?.type as EntityType | undefined;

  // Get the preview renderer
  const PreviewRenderer = useRegisteredEntityRenderer(
    entityType ?? 'rubric',
    'preview'
  );

  return (
    <Tooltip.Root disableHoverablePopup={true}>
      <Tooltip.Trigger render={<span />}>
        {children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner side="right" align="center">
          <Tooltip.Popup
            style={{
              background: '#1a1a2e88',
              border: '1px dashed #555',
              borderRadius: 4,
              padding: 8,
              maxWidth: 300,
              zIndex: 1000,
              boxShadow: '0 4px 16px rgba(0, 0, 0, 0.4)',
            }}
          >
            <CurrentViewContext.Provider value={{ viewId: null, inHoverPreview: true }}>
              <PreviewRenderer entityId={entityId} />
            </CurrentViewContext.Provider>
          </Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

// Re-export Tooltip.Provider for use in GroupSidebar
export const TooltipProvider = Tooltip.Provider;
