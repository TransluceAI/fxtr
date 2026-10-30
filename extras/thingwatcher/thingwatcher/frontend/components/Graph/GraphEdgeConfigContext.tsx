/**
 * GraphEdgeConfigContext - Context for graph edge configuration and port registration.
 *
 * This module provides the Registry instance and types for custom graph node renderers to:
 * 1. Register their ports (connection points) so edges can route to them
 * 2. Register edge configurations (source/target ports, labels, styles)
 *
 * Uses the Registry pattern (see docs/registry_pattern.md) to avoid render loops
 * that would occur with traditional React state.
 *
 * The Graph component provides this context and uses the registered data
 * to configure React Flow edges.
 */

import React, { createContext, useContext, useRef } from 'react';
import { Registry } from '../../lib/Registry';

// =============================================================================
// Types
// =============================================================================

export interface EdgeConfig {
  linkKey: string;
  sourcePort: string;
  targetPort: string | ((availablePorts: string[]) => string | null);
  label?: string;
  style?: React.CSSProperties;
}

export interface PortInfo {
  nodeId: string;
  portId: string;
  type: 'source' | 'target';
}

// Registry keys
export const EDGE_CONFIGS_KEY = 'edgeConfigs';
export const PORTS_KEY = 'ports';

// =============================================================================
// Context
// =============================================================================

const GraphRegistryContext = createContext<Registry | null>(null);

/**
 * Hook to access the graph registry.
 * Returns null if not inside a Graph component (safe for use outside graph).
 */
export function useGraphRegistry(): Registry | null {
  return useContext(GraphRegistryContext);
}

// =============================================================================
// Provider
// =============================================================================

interface GraphEdgeConfigProviderProps {
  children: React.ReactNode;
}

/**
 * Provider component for the graph registry context.
 * Should wrap ReactFlow in the Graph component.
 *
 * Creates the Registry once and never replaces it, ensuring stable context value.
 */
export function GraphEdgeConfigProvider({ children }: GraphEdgeConfigProviderProps) {
  // Create registry once, never replace it
  const registryRef = useRef<Registry | null>(null);
  if (!registryRef.current) {
    registryRef.current = new Registry();
  }

  return (
    <GraphRegistryContext.Provider value={registryRef.current}>
      {children}
    </GraphRegistryContext.Provider>
  );
}
