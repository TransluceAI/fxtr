import { useEffect, useRef, useCallback, useState } from 'react';
import type { Node, Edge } from '@xyflow/react';
import { ColaLayoutManager, type NodeInfo, type EdgeInfo } from './ColaLayoutManager';

export interface ThingNodeData extends Record<string, unknown> {
  thingKey: string;
  keyJSON: unknown;
  shortId: number;
  thingType: string;
}

export interface UseColaLayoutResult {
  /** Current positions for all nodes (reactive - changes trigger re-renders) */
  positions: Map<string, { x: number; y: number }>;
  /** Pin a node at a specific position (for dragging) */
  pinNode: (id: string, x: number, y: number) => void;
  /** Unpin a node (after drag ends) */
  unpinNode: (id: string) => void;
  /** Update position during drag */
  updateDragPosition: (id: string, x: number, y: number) => void;
  /** Update node dimensions (for custom-sized nodes) */
  updateNodeDimensions: (id: string, width: number, height: number) => void;
}

/**
 * React hook that bridges React Flow nodes/edges to the ColaLayoutManager.
 *
 * This hook:
 * - Creates the manager once (persists across renders)
 * - Diffs props and calls manager methods when they change
 * - Returns positions reactively (state updates when cola positions change)
 * - Returns methods for dragging support
 */
export function useColaLayout(
  nodes: Node<ThingNodeData>[],
  edges: Edge[],
  containerSize: { width: number; height: number }
): UseColaLayoutResult {
  // Reactive positions state - updated when cola notifies us
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }>>(new Map());

  // Callback when cola positions change - copy the map to trigger React update
  const onPositionsChanged = useCallback(() => {
    if (managerRef.current) {
      // Create a new Map to trigger React state update
      setPositions(new Map(managerRef.current.getPositions()));
      // console.log("Updated react positions");
    }
  }, []);

  // Create manager once
  const managerRef = useRef<ColaLayoutManager | null>(null);
  if (!managerRef.current) {
    managerRef.current = new ColaLayoutManager(onPositionsChanged);
  }
  const manager = managerRef.current;

  // Update size when container changes
  useEffect(() => {
    manager.setSize(containerSize.width, containerSize.height);
  }, [manager, containerSize.width, containerSize.height]);

  // Sync nodes and edges when they change
  useEffect(() => {
    const nodeInfos: NodeInfo[] = nodes.map((n) => ({
      id: n.id,
      shortId: n.data.shortId,
    }));

    const edgeInfos: EdgeInfo[] = edges.map((e) => ({
      source: e.source,
      target: e.target,
    }));

    manager.syncGraph(nodeInfos, edgeInfos);
  }, [manager, nodes, edges]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      manager.dispose();
    };
  }, [manager]);

  // Return stable callbacks
  const pinNode = useCallback((id: string, x: number, y: number) => manager.pinNode(id, x, y), [manager]);
  const unpinNode = useCallback((id: string) => manager.unpinNode(id), [manager]);
  const updateDragPosition = useCallback(
    (id: string, x: number, y: number) => manager.updateDragPosition(id, x, y),
    [manager]
  );
  const updateNodeDimensions = useCallback(
    (id: string, width: number, height: number) => manager.updateNodeDimensions(id, width, height),
    [manager]
  );

  return {
    positions,
    pinNode,
    unpinNode,
    updateDragPosition,
    updateNodeDimensions,
  };
}
