/**
 * HoverPreview - Hover preview card components
 *
 * HoverPreviewCard: Shows entity preview when hovering over NodeRef/InlineNodeRef
 * BacklinksHoverCard: Shows backlinks when hovering over DraggableIDMarker
 *
 * Both support nested hover cards - hovering items inside the popup keeps the parent open.
 *
 * Breadcrumb behavior:
 * - HoverPreviewCard extends the parent's breadcrumb chain
 * - If parent has no context (e.g., hovering from Graph), starts a NEW chain
 *   with currentKey set to the hovered thing but empty pendingBreadcrumbs
 * - This allows nested hovers to accumulate breadcrumbs even from Graph
 */

import React, { useState, useEffect, useMemo, type ReactNode } from 'react';
import { PreviewCard } from '@base-ui/react/preview-card';
import { HoverKeptOpenContext, useSetKeptOpen, useHoverClose } from './HoverContext';
import {
  useThing,
  useThingTypeConfig,
  useBreadcrumbSource,
  BreadcrumbSourceProvider,
  type BreadcrumbSource,
  type PendingBreadcrumb,
} from '../hooks';
import { DynamicPreviewRenderer } from '../components/DynamicRenderer';
import {
  createPreviewRendererHandle,
  type HandleMetadata,
} from './handleFactory';
import { NodeRef } from './ThingRenderLib';
import styles from './HoverPreview.module.css';

// =============================================================================
// HoverPreviewCard
// =============================================================================

export interface HoverPreviewCardProps {
  /** Thing key (base64) to preview */
  thingKey: string;
  /** Trigger element */
  children: ReactNode;
}

/**
 * Wraps a trigger element and shows an entity preview card on hover.
 * Uses the registered preview renderer for the entity's type.
 * Supports nested hover cards via HoverKeptOpenContext.
 *
 * Extends parent's breadcrumb chain. If parent has no context (e.g., Graph),
 * starts a new chain with the hovered thing as currentKey.
 */
export function HoverPreviewCard({ thingKey, children }: HoverPreviewCardProps) {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);
  const [touchedBeforeClose, setTouchedBeforeClose] = useState(false);
  const parentSetKeptOpen = useSetKeptOpen();
  const hoverClose = useHoverClose();
  const parentContext = useBreadcrumbSource();

  const isOpen = touchedBeforeClose && (open || keptOpenByChild || keptOpenByHover);

  // Build the hover context (extends parent chain or starts new)
  const hoverContext = useMemo((): BreadcrumbSource => {
    if (!parentContext?.currentKey) {
      // No parent context (e.g., hovering from Graph) - start a new chain
      // This allows nested hovers to still accumulate breadcrumbs
      return {
        pendingBreadcrumbs: [],
        currentKey: thingKey,
        type: 'link',
      };
    }

    // Add breadcrumb from parent to this hovered thing
    const newBreadcrumb: PendingBreadcrumb = {
      fromKey: parentContext.currentKey,
      toKey: thingKey,
      type: parentContext.type, // Inherit type from parent
    };

    return {
      pendingBreadcrumbs: [...parentContext.pendingBreadcrumbs, newBreadcrumb],
      currentKey: thingKey,
      type: 'link', // Preview content uses link type
    };
  }, [parentContext, thingKey]);

  // Propagate open state to parent preview card
  useEffect(() => {
    parentSetKeptOpen?.(isOpen);
  }, [isOpen, parentSetKeptOpen]);

  // Register close callback so we can close when navigation happens
  useEffect(() => {
    if (!hoverClose) return;
    return hoverClose.registerCloseListener(() => {
      setTouchedBeforeClose(false);
    });
  }, [hoverClose]);

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger
        delay={500}
        closeDelay={300}
        onMouseEnter={() => setTouchedBeforeClose(true)}
        style={{ minWidth: 0, flex: "1 1 auto" }}
      >
        {children}
      </PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner side="top" align="start" alignOffset={20} sideOffset={8}>
          <PreviewCard.Popup
            onMouseEnter={() => setKeptOpenByHover(true)}
            onMouseLeave={() => setKeptOpenByHover(false)}
            className={styles.hoverPopup}
          >
            <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
              <BreadcrumbSourceProvider value={hoverContext}>
                <PreviewContent thingKey={thingKey} />
              </BreadcrumbSourceProvider>
            </HoverKeptOpenContext.Provider>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}

// =============================================================================
// PreviewContent
// =============================================================================

interface PreviewContentProps {
  thingKey: string;
}

/**
 * Internal component that fetches thing data and renders the preview.
 */
function PreviewContent({ thingKey }: PreviewContentProps) {
  const { thing, loading } = useThing({
    keyInput: { key: thingKey },
    includeData: false,
  });
  const { config: typeConfig } = useThingTypeConfig(thing?.thingType ?? '');

  // Create handle for the preview renderer
  const rendererRef = typeConfig?.renderer ?? null;
  const handle = useMemo(() => {
    if (!thing) return null;
    const metadata: HandleMetadata = {
      key: thing.key,
      keyJSON: thing.keyJSON,
      shortId: thing.shortId,
      thingType: thing.thingType,
      moduleId: rendererRef?.moduleId ?? '',
    };
    return createPreviewRendererHandle(thing.key, metadata);
  }, [thing?.key, thing?.keyJSON, thing?.shortId, thing?.thingType, rendererRef?.moduleId]);

  if (!thing) {
    if (loading) {
      return <div className={styles.loadingPreview}>Loading...</div>;
    } else {
      return <div className={styles.loadingPreview}>Thing not found? {thingKey}</div>;
    }
  }

  if (!handle) {
    return <div className={styles.loadingPreview}>Loading...</div>;
  }

  return (
    <DynamicPreviewRenderer
      thingType={thing.thingType}
      rendererRef={typeConfig?.renderer ?? null}
      handle={handle}
    />
  );
}

// =============================================================================
// BacklinksHoverCard
// =============================================================================

export interface BacklinksHoverCardProps {
  /** Thing key (base64) */
  thingKey: string;
  /** Trigger element */
  children: ReactNode;
}

/**
 * Wraps a trigger element and shows backlinks on hover.
 * Shows "Referenced by:" header with list of backlinks using NodeRef.
 * Supports nested hover cards via HoverKeptOpenContext.
 *
 * Extends parent's breadcrumb chain with 'backlink' type.
 * If parent has no context (e.g., Graph), starts a new chain.
 */
export function BacklinksHoverCard({ thingKey, children }: BacklinksHoverCardProps) {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);
  const [touchedBeforeClose, setTouchedBeforeClose] = useState(false);
  const parentSetKeptOpen = useSetKeptOpen();
  const hoverClose = useHoverClose();
  const parentContext = useBreadcrumbSource();

  const isOpen = touchedBeforeClose && (open || keptOpenByChild || keptOpenByHover);

  // Build the hover context (extends parent chain or starts new)
  const hoverContext = useMemo((): BreadcrumbSource => {
    if (!parentContext?.currentKey) {
      // No parent context - start a new chain (same as HoverPreviewCard)
      return {
        pendingBreadcrumbs: [],
        currentKey: thingKey,
        type: 'backlink',
      };
    }

    // Add breadcrumb from parent to this hovered thing
    const newBreadcrumb: PendingBreadcrumb = {
      fromKey: parentContext.currentKey,
      toKey: thingKey,
      type: parentContext.type,
    };

    return {
      pendingBreadcrumbs: [...parentContext.pendingBreadcrumbs, newBreadcrumb],
      currentKey: thingKey,
      type: 'backlink', // Backlinks section uses backlink type
    };
  }, [parentContext, thingKey]);

  // Propagate open state to parent preview card
  useEffect(() => {
    parentSetKeptOpen?.(isOpen);
  }, [isOpen, parentSetKeptOpen]);

  // Register close callback so we can close when navigation happens
  useEffect(() => {
    if (!hoverClose) return;
    return hoverClose.registerCloseListener(() => {
      setTouchedBeforeClose(false);
    });
  }, [hoverClose]);

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger
        delay={500}
        closeDelay={300}
        onMouseEnter={() => setTouchedBeforeClose(true)}
      >
        {children}
      </PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner side="top" align="start" alignOffset={20} sideOffset={8}>
          <PreviewCard.Popup
            onMouseEnter={() => setKeptOpenByHover(true)}
            onMouseLeave={() => setKeptOpenByHover(false)}
            className={styles.hoverPopup}
          >
            <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
              <BreadcrumbSourceProvider value={hoverContext}>
                <BacklinksContent thingKey={thingKey} />
              </BreadcrumbSourceProvider>
            </HoverKeptOpenContext.Provider>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}

// =============================================================================
// BacklinksContent
// =============================================================================

interface BacklinksContentProps {
  thingKey: string;
}

/**
 * Internal component that fetches backlinks and renders them.
 */
function BacklinksContent({ thingKey }: BacklinksContentProps) {
  // Query thing with backlinks (don't need full data, just backlinks)
  const { thing, loading } = useThing({
    keyInput: { key: thingKey },
    includeData: false,
  });

  if (loading || !thing) {
    return <div className={styles.loadingPreview}>Loading...</div>;
  }

  const backlinks = thing.backlinks ?? [];

  return (
    <div className={styles.backlinksSection}>
      <div className={styles.sectionHeader}>Referenced by:</div>
      {backlinks.length === 0 ? (
        <div className={styles.emptyBacklinks}>(none)</div>
      ) : (
        <div className={styles.backlinksList}>
          {backlinks.map((backlink, i) => (
            <div key={i} className={styles.backlinkItem}>
              <span className={styles.linkType}>{backlink.linkType}:</span>
              <NodeRef thingKey={backlink.fromThing.key} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
