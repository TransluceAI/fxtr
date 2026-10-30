import React, { useCallback } from 'react';
import { ContextMenu } from '@base-ui/react/context-menu';
import { useThingUIEvents } from '../../lib/ThingUIEventsContext';
import styles from './ThingContextMenu.module.css';

interface ThingContextMenuProps {
  /** The thing key this menu is for */
  thingKey: string;
  /** Children to wrap - right-click triggers the menu */
  children: React.ReactNode;
}

/**
 * Context menu wrapper for things - shows "Open in new panel/window" option.
 * Wrap any element in this component to enable right-click context menu.
 *
 * Must be used within a ThingUIEventsProvider.
 *
 * @example
 * ```tsx
 * <ThingContextMenu thingKey={thing.key}>
 *   <div className="list-item">...</div>
 * </ThingContextMenu>
 * ```
 */
export function ThingContextMenu({ thingKey, children }: ThingContextMenuProps) {
  const { onOpenThingInNewPanel, openInNewPanelLabel } = useThingUIEvents();

  const handleOpenInPanel = useCallback(() => {
    onOpenThingInNewPanel(thingKey);
  }, [onOpenThingInNewPanel, thingKey]);

  const menuItemLabel = openInNewPanelLabel === 'panel'
    ? 'Open in new panel'
    : 'Open in new window';

  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger style={{ display: 'contents' }}>
        {children}
      </ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Positioner className={styles.positioner}>
          <ContextMenu.Popup className={styles.popup}>
            <ContextMenu.Item className={styles.item} onClick={handleOpenInPanel}>
              {menuItemLabel}
            </ContextMenu.Item>
          </ContextMenu.Popup>
        </ContextMenu.Positioner>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
