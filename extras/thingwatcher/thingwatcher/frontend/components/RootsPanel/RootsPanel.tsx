import React from 'react';
import type { Root } from '../../types';
import { NodeRef } from '../../lib/ThingRenderLib';
import styles from './RootsPanel.module.css';

interface RootsPanelProps {
  roots: Root[];
  selectedThingKey: string | null;  // b64 key - canonical identifier
  onSelectThing: (key: string) => void;
}

export function RootsPanel({
  roots,
  selectedThingKey,
  onSelectThing,
}: RootsPanelProps) {
  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h3 className={styles.title}>Roots</h3>
        <span className={styles.count}>{roots.length}</span>
      </div>

      <div className={styles.list}>
        {roots.length === 0 ? (
          <div className={styles.empty}>No roots defined</div>
        ) : (
          roots.map(root => (
            <div
              key={root.name}
              className={`${styles.item} ${
                root.thing.key === selectedThingKey ? styles.selected : ''
              }`}
              onClick={() => onSelectThing(root.thing.key)}
            >
              <span className={styles.rootName}>{root.name}</span>
              <span className={styles.arrow}>&rarr;</span>
              <NodeRef thingKey={root.thing.key} />
            </div>
          ))
        )}
      </div>
    </div>
  );
}
