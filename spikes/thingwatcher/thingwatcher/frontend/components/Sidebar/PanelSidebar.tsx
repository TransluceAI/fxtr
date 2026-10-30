/**
 * PanelSidebar - Displays a navigable list of all panel groups and their panels.
 *
 * Shows outside dockview on the left. Thing panels display using the RefRenderer,
 * non-thing panels show just their title. Clicking a panel focuses it.
 */

import React, { useMemo } from 'react';
import type { GroupLayout, PanelLayout } from '../DockviewLayout/usePanelLayout';
import { useThing, useThingTypeConfig } from '../../hooks';
import { DynamicRefRenderer, FallbackRefRenderer } from '../DynamicRenderer';
import { createRefRendererHandle, type HandleMetadata } from '../../lib/handleFactory';
import { SidebarTooltip, TooltipProvider } from './SidebarTooltip';
import styles from './PanelSidebar.module.css';
import { NodeRef } from '../../lib/ThingRenderLib';

export interface PanelSidebarProps {
  layout: GroupLayout[];
  onPanelFocus: (panelId: string) => void;
  onPanelClose: (panelId: string) => void;
  selectedThingKey: string | null;
}

/**
 * Main sidebar component showing all groups and panels.
 */
export function PanelSidebar({ layout, onPanelFocus, onPanelClose, selectedThingKey }: PanelSidebarProps) {
  return (
    <TooltipProvider>
      <div className={styles.container}>
        <div className={styles.header}>Panels</div>
        {layout.map((group, index) => (
          <GroupSection
            key={group.groupId}
            group={group}
            index={index}
            onPanelFocus={onPanelFocus}
            onPanelClose={onPanelClose}
            selectedThingKey={selectedThingKey}
          />
        ))}
      </div>
    </TooltipProvider>
  );
}

interface GroupSectionProps {
  group: GroupLayout;
  index: number;
  onPanelFocus: (panelId: string) => void;
  onPanelClose: (panelId: string) => void;
  selectedThingKey: string | null;
}

function GroupSection({ group, index, onPanelFocus, onPanelClose, selectedThingKey }: GroupSectionProps) {
  return (
    <div className={styles.groupSection}>
      <div className={styles.groupHeader}>Group {index + 1}</div>
      {group.panels.map((panel) => (
        <PanelItem
          key={panel.panelId}
          panel={panel}
          onFocus={() => onPanelFocus(panel.panelId)}
          onClose={() => onPanelClose(panel.panelId)}
          selectedThingKey={selectedThingKey}
        />
      ))}
    </div>
  );
}

interface PanelItemProps {
  panel: PanelLayout;
  onFocus: () => void;
  onClose: () => void;
  selectedThingKey: string | null;
}

function PanelItem({ panel, onFocus, onClose, selectedThingKey }: PanelItemProps) {
  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation(); // Don't trigger onFocus
    onClose();
  };

  const itemClasses = `${styles.panelItem} ${panel.isActive ? styles.panelItemActive : ''}`;

  // Special case: details-main panel shows "Selected: <NodeRef>"
  const isDetailsPanel = panel.panelId === 'details-main';

  const content = (
    <div className={itemClasses} onClick={onFocus}>
      <div className={styles.panelLabel}>
        {isDetailsPanel ? (
          <SelectedThingLabel selectedThingKey={selectedThingKey} />
        ) : panel.thingKey ? (
          <ThingPanelLabel thingKey={panel.thingKey} />
        ) : (
          <span className={styles.textLabel}>{panel.title}</span>
        )}
      </div>
      <button
        className={styles.closeButton}
        onClick={handleClose}
        title="Close panel"
      >
        ×
      </button>
    </div>
  );

  // Wrap thing panels and details panel (when has selection) with tooltip preview
  if (panel.thingKey) {
    return (
      <SidebarTooltip thingKey={panel.thingKey}>
        {content}
      </SidebarTooltip>
    );
  }

  if (isDetailsPanel && selectedThingKey) {
    return (
      <SidebarTooltip thingKey={selectedThingKey}>
        {content}
      </SidebarTooltip>
    );
  }

  return content;
}

interface ThingPanelLabelProps {
  thingKey: string;
}

/**
 * Renders the thing using its RefRenderer (without click/drag behavior).
 */
function ThingPanelLabel({ thingKey }: ThingPanelLabelProps) {
  return (
    <div className={styles.refWrapper}>
      <NodeRef
        thingKey={thingKey}
        args={{}}
      />
    </div>
  );
}

interface SelectedThingLabelProps {
  selectedThingKey: string | null;
}

/**
 * Renders "Selected: <NodeRef>" for the details-main panel.
 * Shows "Selected Thing" if nothing is selected.
 */
function SelectedThingLabel({ selectedThingKey }: SelectedThingLabelProps) {
  if (!selectedThingKey) {
    return (
      <div className={styles.selectedLabel}>
        <span className={styles.selectedPrefix}>Selected:</span>
        <span className={styles.nothingLabel}>(nothing)</span>
      </div>
    );
  }

  return (
    <div className={styles.selectedLabel}>
      <span className={styles.selectedPrefix}>Selected:</span>
      <div className={styles.refWrapper}>
        <ThingPanelLabel thingKey={selectedThingKey} />
      </div>
    </div>
  );
}
