/**
 * DynamicGraphNodeRenderer - Renders custom graph nodes
 *
 * Uses esbuild-wasm to compile TSX code in the browser, then renders
 * the compiled graph renderer component inside a shadow DOM for style isolation.
 *
 * Falls back to FallbackGraphNode if no custom graph renderer is defined.
 */

import React, { Component, type ReactNode, useMemo } from 'react';
import { useThingTypeConfig } from '../../hooks';
import {
  createRefRendererHandle,
  type RefRendererHandle,
  type HandleMetadata,
} from '../../lib/handleFactory';
import { useRenderer } from '../DynamicRenderer';
import { ShadowRootDiv } from '../../lib/ShadowDOM';
import { FallbackGraphNode } from './FallbackGraphNode';

// =============================================================================
// Error Boundary
// =============================================================================

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback: (error: Error) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  render() {
    if (this.state.error) {
      return this.props.fallback(this.state.error);
    }
    return this.props.children;
  }
}

// =============================================================================
// DynamicGraphNodeRenderer Component
// =============================================================================

export interface DynamicGraphNodeRendererProps {
  /** The thing's key (base64) */
  thingKey: string;
  /** The thing type */
  thingType: string;
  /** The thing's short ID */
  shortId: number;
  /** JSON representation of the key (if available) */
  keyJSON?: unknown;
}

/**
 * Renders a custom graph node if one is registered for the thing type,
 * otherwise renders FallbackGraphNode.
 *
 * Uses the same pattern as other DynamicRenderer components:
 * 1. Fetch renderer config for the thing type
 * 2. Compile the renderer module
 * 3. Create a RefRendererHandle
 * 4. Render in shadow DOM with error boundary
 */
export function DynamicGraphNodeRenderer({
  thingKey,
  thingType,
  shortId,
  keyJSON,
}: DynamicGraphNodeRendererProps) {
  // Get renderer config for this thing type
  const { config: typeConfig } = useThingTypeConfig(thingType);
  const rendererRef = typeConfig?.renderer ?? null;

  // Fetch and compile the renderer module
  const { renderers, stylesheet, error, loading } = useRenderer(rendererRef);

  // Create handle for the renderer
  const handle = useMemo<RefRendererHandle>(() => {
    const metadata: HandleMetadata = {
      key: thingKey,
      keyJSON: keyJSON ?? null,
      shortId,
      thingType,
      moduleId: rendererRef?.moduleId ?? '',
    };
    return createRefRendererHandle(thingKey, metadata);
  }, [thingKey, keyJSON, shortId, thingType, rendererRef?.moduleId]);

  // Memoize stylesheet array
  const stylesheets = useMemo(
    () => (stylesheet ? [stylesheet] : undefined),
    [stylesheet]
  );

  // Memoize renderer props
  const rendererProps = useMemo(() => ({ handle }), [handle]);

  // Loading state - show fallback while loading
  if (loading && rendererRef) {
    return <FallbackGraphNode shortId={shortId} thingType={thingType} />;
  }

  // Error state - show fallback (no error UI for compact graph nodes)
  if (error) {
    return <FallbackGraphNode shortId={shortId} thingType={thingType} />;
  }

  // Check if there's a custom graph renderer
  const GraphRenderer = renderers?.graph;
  if (GraphRenderer) {
    return (
      <ErrorBoundary
        fallback={() => <FallbackGraphNode shortId={shortId} thingType={thingType} />}
      >
        <ShadowRootDiv stylesheets={stylesheets} style={{ display: 'contents' }}>
          <GraphRenderer {...rendererProps} />
        </ShadowRootDiv>
      </ErrorBoundary>
    );
  }

  // No custom graph renderer - use fallback
  return <FallbackGraphNode shortId={shortId} thingType={thingType} />;
}
