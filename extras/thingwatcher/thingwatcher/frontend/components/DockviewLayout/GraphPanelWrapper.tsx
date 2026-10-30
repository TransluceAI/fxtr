import React from 'react';
import type { IDockviewPanelProps } from 'dockview';
import { Graph } from '../Graph';
import { useDockviewProps } from './DockviewLayout';
import styles from './DockviewLayout.module.css';

/**
 * Wrapper component that renders Graph inside a dockview panel.
 * Gets props from React context.
 */
export function GraphPanelWrapper(_props: IDockviewPanelProps) {
  const dockviewProps = useDockviewProps();

  return (
    <div className={styles.panelContent}>
      <Graph
        things={dockviewProps.things}
        changeInfo={dockviewProps.changeInfo}
        selectedThingKey={dockviewProps.selectedThingKey}
        onSelectThing={dockviewProps.onSelectThing}
      />
    </div>
  );
}
