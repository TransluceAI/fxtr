/**
 * FallbackGraphNode - Default graph node when no custom renderer is defined
 *
 * Shows a simple colored square with the shortId label.
 * Uses React Flow's Handle components directly (not our Port wrapper)
 * since fallback doesn't need port registration.
 */

import React from 'react';
import { Handle, Position } from '@xyflow/react';
import { useThingTypeConfig } from '../../hooks';
import styles from './Graph.module.css';

// Default color when no config is found
const DEFAULT_COLOR = '#6366f1';

export interface FallbackGraphNodeProps {
  /** The thing's short ID */
  shortId: number;
  /** The thing type (used to get color) */
  thingType: string;
}

/**
 * Fallback graph node - simple colored square with shortId.
 *
 * Uses hidden Handle components for edge connections.
 * The handles connect to node center by default.
 */
export function FallbackGraphNode({ shortId, thingType }: FallbackGraphNodeProps) {
  const { config } = useThingTypeConfig(thingType);
  const color = config?.color ?? DEFAULT_COLOR;

  return (
    <div className={styles.fallbackNode} style={{ backgroundColor: color }}>
      {/* Hidden handles for edge connections */}
      <Handle
        type="target"
        position={Position.Top}
        className={styles.handle}
      />
      <span className={styles.thingNodeLabel}>{shortId}</span>
      <Handle
        type="source"
        position={Position.Bottom}
        className={styles.handle}
      />
    </div>
  );
}
