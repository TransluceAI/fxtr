import React from 'react';
import type { IDockviewPanelProps } from 'dockview';
import { ThingsList } from '../ThingsList';
import { useDockviewProps } from './DockviewLayout';
import styles from './DockviewLayout.module.css';

/**
 * Wrapper component that renders ThingsList inside a dockview panel.
 */
export function ThingsListPanelWrapper(_props: IDockviewPanelProps) {
  const dockviewProps = useDockviewProps();

  return (
    <div className={styles.panelContent}>
      <ThingsList
        things={dockviewProps.things}
        changeInfo={dockviewProps.changeInfo}
        selectedThingKey={dockviewProps.selectedThingKey}
        onSelectThing={dockviewProps.onSelectThing}
        isLive={dockviewProps.isLive}
        triggeredShortIds={dockviewProps.triggeredShortIds}
      />
    </div>
  );
}
