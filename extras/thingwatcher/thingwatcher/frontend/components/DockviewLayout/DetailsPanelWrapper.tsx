import React from 'react';
import type { IDockviewPanelProps } from 'dockview';
import { DetailsPanel } from '../DetailsPanel';
import { useDockviewProps } from './DockviewLayout';
import styles from './DockviewLayout.module.css';

/**
 * Wrapper component that renders DetailsPanel inside a dockview panel.
 * Shows the currently selected thing (from app state).
 */
export function DetailsPanelWrapper(_props: IDockviewPanelProps) {
  const dockviewProps = useDockviewProps();

  // Check if selected thing is disappearing
  const isDisappearing = dockviewProps.selectedThingKey
    ? dockviewProps.changeInfo.disappearingKeys.has(dockviewProps.selectedThingKey)
    : false;

  return (
    <div className={styles.panelContent}>
      <DetailsPanel
        thingKey={dockviewProps.selectedThingKey}
        onSelectThing={dockviewProps.onSelectThing}
        triggerRecords={dockviewProps.triggerRecords}
        onTrigger={dockviewProps.onTrigger}
        isDisappearing={isDisappearing}
      />
    </div>
  );
}
