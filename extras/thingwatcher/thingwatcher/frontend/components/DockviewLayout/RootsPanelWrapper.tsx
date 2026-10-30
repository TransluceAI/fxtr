import React from 'react';
import type { IDockviewPanelProps } from 'dockview';
import { RootsPanel } from '../RootsPanel';
import { useDockviewProps } from './DockviewLayout';
import styles from './DockviewLayout.module.css';

/**
 * Wrapper component that renders RootsPanel inside a dockview panel.
 */
export function RootsPanelWrapper(_props: IDockviewPanelProps) {
  const dockviewProps = useDockviewProps();

  return (
    <div className={styles.panelContent}>
      <RootsPanel
        roots={dockviewProps.roots}
        selectedThingKey={dockviewProps.selectedThingKey}
        onSelectThing={dockviewProps.onSelectThing}
      />
    </div>
  );
}
