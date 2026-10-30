import React, { useMemo, useCallback, useEffect } from 'react';
import type { IDockviewPanelProps } from 'dockview';
import { useDockviewProps } from './DockviewLayout';
import { useThing, useTriggerRecords } from '../../hooks';
import { ThingPanelContent } from '../ThingPanelContent';
import styles from './DockviewLayout.module.css';

interface ThingViewParams {
  key: string;  // b64 key - canonical identifier
}

/**
 * Panel component for viewing a specific thing.
 * Unlike DetailsPanelWrapper, this panel is for a fixed thing (opened via context menu).
 * It still tracks the current tick, so the data updates as time changes.
 *
 * Active marking is handled by ThingPanelContent via the markSource prop.
 */
export function ThingViewPanel(props: IDockviewPanelProps<ThingViewParams>) {
  const { key } = props.params;
  const dockviewProps = useDockviewProps();
  const currentTickIndex = dockviewProps.currentTickIndex;

  // Query thing details by key at current tick for panel title
  // ThingPanelContent also fetches this, but it's cached via graphcache
  const { thing } = useThing({
    keyInput: { key },
    includeData: false,  // Just need shortId/thingType for title
  });

  // Get all trigger records using hook
  const { records: allTriggerRecords } = useTriggerRecords();

  // Filter trigger records for this thing at current tick
  const triggerRecords = useMemo(() => {
    return allTriggerRecords.filter(
      tr => tr.thing.key === key && tr.triggerTick === currentTickIndex
    );
  }, [allTriggerRecords, key, currentTickIndex]);

  // Check if this thing is disappearing
  const isDisappearing = dockviewProps.changeInfo.disappearingKeys.has(key);

  // Update panel title when thing loads
  useEffect(() => {
    if (thing) {
      props.api.setTitle(`#${thing.shortId} ${thing.thingType}`);
    }
  }, [thing, props.api]);

  // Callback after trigger mutation
  const handleTrigger = useCallback(() => {
    // Trigger records query will auto-refetch via cache invalidation
  }, []);

  return (
    <div className={styles.panelContent}>
      <ThingPanelContent
        thingKey={key}
        onSelectThing={dockviewProps.onSelectThing}
        triggerRecords={triggerRecords}
        onTrigger={handleTrigger}
        isDisappearing={isDisappearing}
        markSource="panel"
        markLabel={props.api.id}
      />
    </div>
  );
}
