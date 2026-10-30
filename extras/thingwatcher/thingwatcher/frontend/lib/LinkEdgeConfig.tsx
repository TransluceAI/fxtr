/**
 * LinkEdgeConfig - Declarative edge configuration for custom graph nodes.
 *
 * This component renders nothing visible - it registers edge configuration
 * with the graph registry so that Graph.tsx can apply the config
 * to the corresponding React Flow edge.
 */

import { useMemo } from 'react';
import { useNodeId } from '@xyflow/react';
import { useRegister } from './Registry';
import {
  useGraphRegistry,
  EDGE_CONFIGS_KEY,
  type EdgeConfig,
} from '../components/Graph/GraphEdgeConfigContext';

export interface LinkEdgeConfigProps {
  /** The link key (base64) this config applies to */
  linkKey: string;
  /** Source port ID on this node */
  sourcePort: string;
  /** Target port ID on the target node, or function to pick from available ports */
  targetPort: string | ((availablePorts: string[]) => string | null);
  /** Text label for the edge (optional) */
  label?: string;
  /** CSS styles for the edge path (optional) */
  style?: React.CSSProperties;
}

/**
 * LinkEdgeConfig component - Registers edge configuration with the graph.
 *
 * Use inside a GraphNodeRenderer to customize how outgoing edges appear.
 * Renders nothing visible.
 *
 * Note: If props.style is a new object each render, this will re-register.
 * Callers should memoize style objects for best performance.
 */
export function LinkEdgeConfig(props: LinkEdgeConfigProps) {
  const nodeId = useNodeId();
  const registry = useGraphRegistry();

  // Memoize the edge config to avoid re-registration on every render
  const edgeConfig = useMemo<EdgeConfig | null>(
    () =>
      nodeId
        ? {
            linkKey: props.linkKey,
            sourcePort: props.sourcePort,
            targetPort: props.targetPort,
            label: props.label,
            style: props.style,
          }
        : null,
    [nodeId, props.linkKey, props.sourcePort, props.targetPort, props.label, props.style]
  );

  useRegister(registry, EDGE_CONFIGS_KEY, edgeConfig);

  // Renders nothing
  return null;
}
