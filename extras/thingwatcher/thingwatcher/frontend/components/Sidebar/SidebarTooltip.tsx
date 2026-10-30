/**
 * SidebarTooltip - Simple tooltip for sidebar entity items.
 *
 * Uses Tooltip instead of PreviewCard for simpler behavior:
 * - Appears to the right of the trigger (sidebar is on left edge)
 * - Not hoverable (disableHoverablePopup)
 * - Uses TooltipProvider for grouped delay behavior
 */

import React, { useMemo, type ReactNode } from 'react';
import { Tooltip } from '@base-ui/react/tooltip';
import { useThing, useThingTypeConfig } from '../../hooks';
import { DynamicPreviewRenderer } from '../DynamicRenderer';
import { createPreviewRendererHandle, type HandleMetadata } from '../../lib/handleFactory';
import styles from './SidebarTooltip.module.css';

// Re-export Tooltip.Provider for use in PanelSidebar
export const TooltipProvider = Tooltip.Provider;

export interface SidebarTooltipProps {
  /** Thing key (base64) to preview */
  thingKey: string;
  /** Trigger element */
  children: ReactNode;
}

/**
 * Simple tooltip wrapper for sidebar thing items.
 * Uses Tooltip instead of PreviewCard for simpler behavior (no hoverable popup).
 * Should be used inside a TooltipProvider for grouped delay behavior.
 */
export function SidebarTooltip({ thingKey, children }: SidebarTooltipProps) {
  return (
    <Tooltip.Root disableHoverablePopup={true}>
      <Tooltip.Trigger render={<span style={{ display: 'block' }} />}>
        {children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner side="right" align="center" sideOffset={8}>
          <Tooltip.Popup className={styles.tooltipPopup}>
            <SidebarPreviewContent thingKey={thingKey} />
          </Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

interface SidebarPreviewContentProps {
  thingKey: string;
}

/**
 * Internal component that fetches thing data and renders the preview.
 */
function SidebarPreviewContent({ thingKey }: SidebarPreviewContentProps) {
  const { thing, loading } = useThing({
    keyInput: { key: thingKey },
    includeData: false, // Handle fetches data via hooks
  });
  const { config: typeConfig } = useThingTypeConfig(thing?.thingType ?? '');

  // Create handle for the renderer
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

  if (!thing || !handle) {
    if (loading) {
      return <div className={styles.loadingPreview}>Loading...</div>;
    } else {
      return <div className={styles.loadingPreview}>Not found</div>;
    }
  }

  return (
    <DynamicPreviewRenderer
      thingType={thing.thingType}
      rendererRef={typeConfig?.renderer ?? null}
      handle={handle}
    />
  );
}
