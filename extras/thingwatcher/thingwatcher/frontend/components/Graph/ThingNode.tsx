import React, { memo, useContext, useState, useCallback } from 'react';
import type { NodeProps, Node } from '@xyflow/react';
import type { ThingNodeData } from './useColaLayout';
import { DynamicGraphNodeRenderer } from './DynamicGraphNodeRenderer';
import { AltKeyContext } from './Graph';
import { HoverPreviewCard } from '../../lib/HoverPreview';
import { setThingDragData, type ThingDropInfo } from '../../lib/dragConstants';
import { useActiveKeys } from '../../hooks';
import styles from './Graph.module.css';

// Change state type
export type ChangeState =
  | 'unchanged'
  | 'added'
  | 'modified'
  | 'disappearing'
  | 'addedDisappearing'
  | 'modifiedDisappearing';

// Extended node data with selection and change state
export interface ThingNodeDataWithState extends ThingNodeData {
  selected?: boolean;
  changeState?: ChangeState;
}

// Full node type for React Flow
type ThingNodeType = Node<ThingNodeDataWithState>;

/**
 * Custom React Flow node for rendering things.
 *
 * This is a wrapper component that:
 * 1. Applies change state styling (colored borders)
 * 2. Applies selection styling (white box-shadow)
 * 3. Renders the actual content via DynamicGraphNodeRenderer
 * 4. Handles Alt+drag for drag-to-panel functionality
 * 5. Shows hover preview card
 *
 * The DynamicGraphNodeRenderer handles loading custom graph renderers
 * or falling back to FallbackGraphNode.
 */
export const ThingNode = memo(function ThingNode({
  data,
}: NodeProps<ThingNodeType>) {
  const { thingKey, keyJSON, shortId, thingType, selected, changeState = 'unchanged' } = data as ThingNodeDataWithState;

  // Alt key state from Graph context
  const altHeld = useContext(AltKeyContext);

  // Get stability status for this thing
  const activeKeys = useActiveKeys();
  const stability = activeKeys.get(thingKey);
  const isStable = stability === 'stable';

  // Track hover state for drag hint tooltip
  const [hovered, setHovered] = useState(false);

  // HTML drag handler (same as NodeRef) - for drag-to-panel or drop regions
  const handleDragStart = useCallback(
    (e: React.DragEvent) => {
      const dropInfo: ThingDropInfo = {
        key: thingKey,
        keyJSON,
        shortId,
        thingType,
      };
      setThingDragData(e.dataTransfer, dropInfo);
    },
    [thingKey, keyJSON, shortId, thingType]
  );

  // Build class names for change state, selection, and stability
  const classNames = [styles.thingNode];
  if (changeState !== 'unchanged') {
    classNames.push(styles[changeState]);
  }
  if (selected) {
    classNames.push(styles.selected);
  }
  if (!isStable) {
    classNames.push(styles.transient);
  }

  return (
    <HoverPreviewCard thingKey={thingKey}>
      <div
        className={classNames.join(' ')}
        draggable={altHeld}
        onDragStart={altHeld ? handleDragStart : undefined}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      >
        {/* Drag hint tooltip - shown when Alt held and hovered */}
        {altHeld && hovered && (
          <div className={styles.dragHint}>Drag to open in panel</div>
        )}

        <DynamicGraphNodeRenderer
          thingKey={thingKey}
          thingType={thingType}
          shortId={shortId}
          keyJSON={keyJSON}
        />
      </div>
    </HoverPreviewCard>
  );
});
