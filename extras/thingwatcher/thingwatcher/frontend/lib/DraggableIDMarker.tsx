/**
 * DraggableIDMarker - Wrapper component for making thing IDs draggable
 *
 * Wraps children and makes them draggable. When dropped on the dockview layout,
 * creates a new panel showing that thing. Can also be dropped into custom
 * ThingDropRegion components in renderers.
 * Hovering shows backlinks to this thing.
 *
 * Breadcrumb behavior:
 * - DraggableIDMarker represents the current thing (not a reference to another thing)
 * - When dragged, includes the pending breadcrumbs from the context WITHOUT adding
 *   a new step (unlike NodeRef which adds itself to the chain)
 */

import React, { useCallback } from 'react';
import { setThingDragData, type ThingDropInfo } from './dragConstants';
import { BacklinksHoverCard } from './HoverPreview';
import { useBreadcrumbSource } from '../hooks';

interface DraggableIDMarkerProps {
  children: React.ReactNode;
}

/** Metadata needed to create a DraggableIDMarker */
export interface DraggableIDMarkerMetadata {
  key: string;
  keyJSON: unknown;
  shortId: number;
  thingType: string;
}

/**
 * Internal DraggableIDMarker component that uses hooks for breadcrumb context.
 */
function DraggableIDMarkerComponent({
  children,
  metadata,
}: DraggableIDMarkerProps & { metadata: DraggableIDMarkerMetadata }) {
  const bcSource = useBreadcrumbSource();

  const handleDragStart = useCallback((e: React.DragEvent) => {
    // Include pending breadcrumbs from context (but don't add a new step)
    // DraggableIDMarker represents the current thing, not a reference to another
    const dropInfo: ThingDropInfo = {
      key: metadata.key,
      keyJSON: metadata.keyJSON,
      shortId: metadata.shortId,
      thingType: metadata.thingType,
      pendingBreadcrumbs: bcSource?.pendingBreadcrumbs ?? [],
    };
    setThingDragData(e.dataTransfer, dropInfo);
  }, [metadata.key, metadata.keyJSON, metadata.shortId, metadata.thingType, bcSource]);

  return (
    <BacklinksHoverCard thingKey={metadata.key}>
      <span
        draggable
        onDragStart={handleDragStart}
        style={{ cursor: 'grab', userSelect: 'none' }}
      >
        {children}
      </span>
    </BacklinksHoverCard>
  );
}

/**
 * Creates a DraggableIDMarker component bound to a specific thing.
 * The returned component wraps its children and makes them draggable.
 * Dropping on dockview creates a new ThingViewPanel for this thing.
 * Dropping on a ThingDropRegion passes the thing info to the renderer.
 * Hovering shows backlinks to this thing.
 */
export function createDraggableIDMarker(metadata: DraggableIDMarkerMetadata): React.FC<DraggableIDMarkerProps> {
  return function DraggableIDMarker({ children }: DraggableIDMarkerProps) {
    return <DraggableIDMarkerComponent metadata={metadata}>{children}</DraggableIDMarkerComponent>;
  };
}
