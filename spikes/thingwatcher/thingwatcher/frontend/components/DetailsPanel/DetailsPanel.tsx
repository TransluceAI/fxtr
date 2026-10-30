/**
 * DetailsPanel - Thin wrapper for showing the currently selected thing
 *
 * This is now a thin wrapper around ThingPanelContent that just adds
 * the "Details" header and handles the no-selection case.
 */

import React from 'react';
import type { TriggerRecord } from '../../types';
import { ThingPanelContent } from '../ThingPanelContent';
import styles from './DetailsPanel.module.css';

interface DetailsPanelProps {
  /** Key of the selected thing (null if nothing selected) */
  thingKey: string | null;
  /** Callback when user clicks a link/backlink to navigate to another thing */
  onSelectThing: (key: string) => void;
  /** Trigger records at the current tick for the selected thing */
  triggerRecords: TriggerRecord[];
  /** Callback after a successful trigger */
  onTrigger: () => void;
  /** Whether this thing will disappear in the next tick */
  isDisappearing?: boolean;
}

export function DetailsPanel({
  thingKey,
  onSelectThing,
  triggerRecords,
  onTrigger,
  isDisappearing = false,
}: DetailsPanelProps) {
  if (!thingKey) {
    return (
      <div className={styles.panel}>
        <div className={styles.header}>
          <h3 className={styles.title}>Details</h3>
        </div>
        <div className={styles.content}>
          <div className={styles.empty}>Select a thing to view details</div>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h3 className={styles.title}>Details</h3>
      </div>
      <ThingPanelContent
        thingKey={thingKey}
        onSelectThing={onSelectThing}
        triggerRecords={triggerRecords}
        onTrigger={onTrigger}
        isDisappearing={isDisappearing}
        markSource="selection"
      />
    </div>
  );
}
