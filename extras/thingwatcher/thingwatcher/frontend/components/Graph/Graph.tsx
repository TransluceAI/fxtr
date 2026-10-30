import React, { useMemo, useCallback, useEffect, useRef, useState, createContext } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  MarkerType,
  useReactFlow,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeChange,
  applyNodeChanges,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import type { Thing, ChangeInfo } from '../../types';
import { ThingNode, type ThingNodeDataWithState, type ChangeState } from './ThingNode';
import { useColaLayout, type ThingNodeData } from './useColaLayout';
import { useRegistrySnapshot } from '../../lib/Registry';
import {
  GraphEdgeConfigProvider,
  useGraphRegistry,
  EDGE_CONFIGS_KEY,
  PORTS_KEY,
  type EdgeConfig,
  type PortInfo,
} from './GraphEdgeConfigContext';
import styles from './Graph.module.css';

// =============================================================================
// Alt Key Context - for drag-to-panel functionality
// =============================================================================

/**
 * Context to share Alt key state with ThingNode components.
 * When Alt is held, nodes become draggable for drag-to-panel instead of graph movement.
 */
export const AltKeyContext = createContext(false);

// Default edge color
const DEFAULT_EDGE_COLOR = '#4b5563';

// Max zoom for viewport
const MAX_ZOOM = 1.5;

// Register custom node types
const nodeTypes = {
  thing: ThingNode,
};

// Edge data includes linkKey for config lookup
interface ThingEdgeData extends Record<string, unknown> {
  linkKey: string;
  linkType: string;
}

interface GraphProps {
  things: Thing[];
  changeInfo: ChangeInfo;
  selectedThingKey: string | null;
  onSelectThing: (key: string | null) => void;
}

/**
 * Compute change state for a thing based on changeInfo.
 */
function getChangeState(key: string, changeInfo: ChangeInfo): ChangeState {
  const isAdded = changeInfo.addedKeys.has(key);
  const isModified = changeInfo.modifiedKeys.has(key);
  const isDisappearing = changeInfo.disappearingKeys.has(key);

  if (isAdded && isDisappearing) return 'addedDisappearing';
  if (isModified && isDisappearing) return 'modifiedDisappearing';
  if (isAdded) return 'added';
  if (isModified) return 'modified';
  if (isDisappearing) return 'disappearing';
  return 'unchanged';
}

/**
 * Convert things array to React Flow base nodes and edges.
 */
function useGraphData(things: Thing[]): {
  baseNodes: Node<ThingNodeData>[];
  edges: Edge<ThingEdgeData>[];
} {
  return useMemo(() => {
    // Build set of valid thing keys for edge validation
    const thingKeySet = new Set(things.map((t) => t.key));

    const baseNodes: Node<ThingNodeData>[] = things.map((thing) => ({
      id: thing.key,
      type: 'thing',
      position: { x: 0, y: 0 }, // Will be set from cola positions
      data: {
        thingKey: thing.key,
        keyJSON: thing.keyJSON,
        shortId: thing.shortId,
        thingType: thing.thingType,
      },
    }));

    const edges: Edge<ThingEdgeData>[] = [];
    for (const thing of things) {
      for (const link of thing.links) {
        // Only create edge if target exists
        if (thingKeySet.has(link.toThing.key)) {
          edges.push({
            id: `${thing.key}-${link.toThing.key}-${link.linkType}-${link.linkKey}`,
            source: thing.key,
            target: link.toThing.key,
            markerEnd: { type: MarkerType.ArrowClosed },
            style: {
              stroke: DEFAULT_EDGE_COLOR,
              opacity: 0.7,
            },
            data: {
              linkKey: link.linkKey,
              linkType: link.linkType,
            },
          });
        }
      }
    }

    return { baseNodes, edges };
  }, [things]);
}

/**
 * Inner Graph component (must be inside ReactFlowProvider).
 */
function GraphInner({ things, changeInfo, selectedThingKey, onSelectThing }: GraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerSize, setContainerSize] = useState({ width: 0, height: 0 });
  const { fitView } = useReactFlow();

  // Track Alt key state for drag-to-panel functionality
  const [altHeld, setAltHeld] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Alt') setAltHeld(true);
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Alt') setAltHeld(false);
    };
    // Reset on blur in case Alt is released while window unfocused
    const handleBlur = () => setAltHeld(false);
    // Sync Alt state during drag (keyup doesn't fire during HTML drag operations)
    const handleDrag = (e: DragEvent) => setAltHeld(e.altKey);

    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('keyup', handleKeyUp);
    window.addEventListener('blur', handleBlur);
    window.addEventListener('drag', handleDrag);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('keyup', handleKeyUp);
      window.removeEventListener('blur', handleBlur);
      window.removeEventListener('drag', handleDrag);
    };
  }, []);

  // Track if user has interacted with viewport
  const userInteractedRef = useRef(false);
  const prevThingsRef = useRef<Thing[]>([]);

  // Reset user interaction flag when data changes significantly
  useEffect(() => {
    const prevKeys = new Set(prevThingsRef.current.map((t) => t.key));
    const currKeys = new Set(things.map((t) => t.key));
    const hasNewThings = things.some((t) => !prevKeys.has(t.key));
    const hasRemovedThings = prevThingsRef.current.some((t) => !currKeys.has(t.key));

    if (hasNewThings || hasRemovedThings) {
      userInteractedRef.current = false;
    }

    prevThingsRef.current = things;
  }, [things]);

  // Measure container size
  useEffect(() => {
    if (!containerRef.current) return;

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) {
        setContainerSize({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });

    resizeObserver.observe(containerRef.current);
    return () => resizeObserver.disconnect();
  }, []);

  // Get base graph data from things
  const { baseNodes, edges: baseEdges } = useGraphData(things);

  // Cola layout hook - returns reactive positions and drag methods
  const { positions, pinNode, unpinNode, updateDragPosition, updateNodeDimensions } = useColaLayout(
    baseNodes,
    baseEdges,
    containerSize
  );

  // ReactFlow state for nodes and edges
  // ReactFlow needs to own this state for internal features (measured, etc.)
  const [nodes, setNodes] = useNodesState<Node<ThingNodeDataWithState>>([]);
  const [edges, setEdges] = useEdgesState<Edge<ThingEdgeData>>(baseEdges);

  // Get edge configs and ports from registry using useSyncExternalStore
  const registry = useGraphRegistry();
  const edgeConfigs = useRegistrySnapshot<EdgeConfig>(registry, EDGE_CONFIGS_KEY);
  const ports = useRegistrySnapshot<PortInfo>(registry, PORTS_KEY);

  // Helper to get target ports for a node
  const getTargetPorts = useCallback(
    (nodeId: string) =>
      ports.filter((p) => p.nodeId === nodeId && p.type === 'target').map((p) => p.portId),
    [ports]
  );

  // Track previous values to detect changes during render (set-state-in-render pattern)
  // This avoids useEffect delay which causes sync issues
  const [prevSyncDeps, setPrevSyncDeps] = React.useState<{
    baseNodes: Node<ThingNodeData>[];
    positions: Map<string, { x: number; y: number }>;
    selectedThingKey: string | null;
    changeInfo: ChangeInfo;
  } | null>(null);

  // Sync edges when base edges change or edge configs change (set-state-in-render)
  const [prevEdgeSyncDeps, setPrevEdgeSyncDeps] = React.useState<{
    baseEdges: Edge<ThingEdgeData>[];
    edgeConfigs: EdgeConfig[];
  } | null>(null);

  const edgeSyncDepsChanged =
    prevEdgeSyncDeps === null ||
    prevEdgeSyncDeps.baseEdges !== baseEdges ||
    prevEdgeSyncDeps.edgeConfigs !== edgeConfigs;

  if (edgeSyncDepsChanged) {
    setPrevEdgeSyncDeps({ baseEdges, edgeConfigs });

    // Build lookup: linkKey -> config
    const configByLinkKey = new Map<string, EdgeConfig>();
    for (const config of edgeConfigs) {
      configByLinkKey.set(config.linkKey, config);
    }

    // Apply edge configs
    const configuredEdges = baseEdges.map((edge) => {
      if (!edge.data?.linkKey) return edge;

      const config = configByLinkKey.get(edge.data.linkKey);
      if (!config) return edge;

      // Resolve targetPort if it's a function
      let targetPort: string | null = null;
      if (typeof config.targetPort === 'function') {
        targetPort = config.targetPort(getTargetPorts(edge.target));
      } else {
        targetPort = config.targetPort;
      }

      return {
        ...edge,
        sourceHandle: config.sourcePort,
        targetHandle: targetPort,
        label: config.label,
        style: config.style ? { ...edge.style, ...config.style } : edge.style,
      };
    });

    setEdges(configuredEdges);
  }

  // Sync nodes from cola positions and update data attributes (set-state-in-render)
  // Check if any dependency changed
  const depsChanged =
    prevSyncDeps === null ||
    prevSyncDeps.baseNodes !== baseNodes ||
    prevSyncDeps.positions !== positions ||
    prevSyncDeps.selectedThingKey !== selectedThingKey ||
    prevSyncDeps.changeInfo !== changeInfo;

  if (depsChanged) {
    // console.log('[graph] depsChanged');
    // Update tracked deps
    setPrevSyncDeps({ baseNodes, positions, selectedThingKey, changeInfo });

    // Build updated nodes synchronously
    const currentNodeMap = new Map(nodes.map((n) => [n.id, n]));
    const updatedNodes = baseNodes.map((baseNode) => {
      const pos = positions.get(baseNode.id) ?? { x: 0, y: 0 };
      const existing = currentNodeMap.get(baseNode.id);

      // Preserve ReactFlow internal state (measured, etc.) from existing node
      return {
        ...existing, // Preserve internal ReactFlow state
        ...baseNode, // Apply base node data
        position: pos, // Override with cola position
        data: {
          ...baseNode.data,
          selected: baseNode.id === selectedThingKey,
          changeState: getChangeState(baseNode.id, changeInfo),
        },
      } as Node<ThingNodeDataWithState>;
    });

    setNodes(updatedNodes);
  }

  // Handle node click for selection
  const onNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node<ThingNodeDataWithState>) => {
      onSelectThing(node.id);
    },
    [onSelectThing]
  );

  // Handle pane click to deselect
  const onPaneClick = useCallback(() => {
    onSelectThing(null);
  }, [onSelectThing]);

  // Handle viewport interactions (user took control)
  const onMoveEnd = useCallback(() => {
    userInteractedRef.current = true;
  }, []);

  // Track which nodes are currently being dragged
  const draggingNodesRef = useRef<Set<string>>(new Set());

  // Handle all node changes from React Flow
  // Position changes go to cola, dimension changes go to cola, others go to ReactFlow state
  const onNodesChange = useCallback(
    (changes: NodeChange<Node<ThingNodeDataWithState>>[]) => {
      // Separate position changes from other changes
      const positionChanges: NodeChange<Node<ThingNodeDataWithState>>[] = [];
      const otherChanges: NodeChange<Node<ThingNodeDataWithState>>[] = [];

      for (const change of changes) {
        if (change.type === 'position') {
          positionChanges.push(change);
        } else {
          otherChanges.push(change);

          // Handle dimension changes - propagate to cola for layout
          if (change.type === 'dimensions' && change.dimensions) {
            updateNodeDimensions(change.id, change.dimensions.width, change.dimensions.height);
          }
        }
      }

      // Handle position changes - send to cola
      for (const change of positionChanges) {
        if (change.type === 'position' && change.position) {
          const { id, position, dragging } = change;

          // Detect drag start
          if (dragging && !draggingNodesRef.current.has(id)) {
            draggingNodesRef.current.add(id);
            pinNode(id, position.x, position.y);
          }

          // Update position during drag
          if (dragging) {
            updateDragPosition(id, position.x, position.y);
          }

          // Detect drag end
          if (!dragging && draggingNodesRef.current.has(id)) {
            draggingNodesRef.current.delete(id);
            updateDragPosition(id, position.x, position.y);
            unpinNode(id);
          }
        }
      }

      // Apply other changes (like 'dimensions') directly to ReactFlow state
      if (otherChanges.length > 0) {
        setNodes((nds) => applyNodeChanges(otherChanges, nds));
      }
    },
    [pinNode, unpinNode, updateDragPosition, updateNodeDimensions, setNodes]
  );

  // Viewport management: fit changed nodes if they're off-screen
  useEffect(() => {
    if (nodes.length === 0) return;
    if (userInteractedRef.current) return;

    // Collect changed node IDs
    const changedNodeIds = new Set<string>();
    for (const key of changeInfo.addedKeys) changedNodeIds.add(key);
    for (const key of changeInfo.modifiedKeys) changedNodeIds.add(key);
    for (const key of changeInfo.disappearingKeys) changedNodeIds.add(key);

    // Small delay to let positions settle
    const timer = setTimeout(() => {
      if (changedNodeIds.size > 0) {
        const changedNodes = nodes.filter((n) => changedNodeIds.has(n.id));
        if (changedNodes.length > 0) {
          fitView({
            nodes: changedNodes,
            padding: 0.2,
            maxZoom: MAX_ZOOM,
            duration: 300,
          });
        }
      } else if (nodes.length > 0) {
        // Initial load - fit all nodes
        fitView({
          padding: 0.2,
          maxZoom: MAX_ZOOM,
          duration: 300,
        });
      }
    }, 100);

    return () => clearTimeout(timer);
  }, [nodes, changeInfo, fitView]);

  if (things.length === 0) {
    return (
      <div className={styles.container} ref={containerRef}>
        <div className={styles.empty}>No things to display</div>
      </div>
    );
  }

  return (
    <div className={styles.container} ref={containerRef}>
      <AltKeyContext.Provider value={altHeld}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          onMoveEnd={onMoveEnd}
          // View-only: disable connections
          nodesConnectable={false}
          connectOnClick={false}
          // Disable React Flow dragging and panning when Alt held (for drag-to-panel)
          nodesDraggable={!altHeld}
          panOnDrag={!altHeld}
          // Max zoom
          maxZoom={MAX_ZOOM}
          minZoom={0.1}
          // Disable default controls
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#1a1a3a" />
        </ReactFlow>
      </AltKeyContext.Provider>
    </div>
  );
}

/**
 * Graph visualization using React Flow with cola.js force-directed layout.
 *
 * Architecture:
 * - ColaLayoutManager owns all layout state (persists across renders)
 * - useColaLayout is a thin React bridge that syncs props to the manager
 * - This component computes positionedNodes directly from cola positions (no useState for nodes)
 * - React Flow is fully controlled - just renders what cola says
 *
 * Features:
 * - Force-directed layout that settles naturally
 * - Position persistence across tick changes
 * - Click to select (white box-shadow)
 * - Change state visualization (colored borders)
 * - Viewport tracking for changed nodes
 * - Custom graph node renderers via GraphEdgeConfigContext
 */
export function Graph(props: GraphProps) {
  return (
    <ReactFlowProvider>
      <GraphEdgeConfigProvider>
        <GraphInner {...props} />
      </GraphEdgeConfigProvider>
    </ReactFlowProvider>
  );
}
