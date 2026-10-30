/**
 * LowLevelNodeView - Internal component for rendering a thing with its FullRenderer
 *
 * This is the core internal component that handles:
 * - Data fetching via hooks (for metadata only - renderer uses hooks via handle)
 * - Handle creation
 * - Rendering with DynamicFullRenderer
 *
 * It does NOT include panel chrome (meta header, backlinks, etc.)
 */

import React, { useMemo } from 'react';
import { useThing, useThingTypeConfig } from '../hooks';
import { DynamicFullRenderer } from '../components/DynamicRenderer';
import {
  createFullRendererHandle,
  type HandleMetadata,
} from './handleFactory';

// =============================================================================
// Types
// =============================================================================

export interface LowLevelNodeViewProps {
  /** Thing key as base64 string */
  thingKey: string;
  /** True if this is a top-level panel view, false if nested */
  isTopLevel: boolean;
  /** Custom args to pass to the renderer */
  args?: Record<string, unknown>;
}

// =============================================================================
// Component
// =============================================================================

export function LowLevelNodeView({
  thingKey,
  isTopLevel,
  args = {},
}: LowLevelNodeViewProps) {
  // Fetch thing metadata (not data - renderer will fetch via hooks)
  const { thing, loading, error } = useThing({
    keyInput: { key: thingKey },
    includeData: false,
  });

  // Get renderer config for this thing type
  const { config: typeConfig } = useThingTypeConfig(thing?.thingType ?? '');
  const rendererRef = typeConfig?.renderer ?? null;

  // Create handle for the renderer
  // This must be after other hooks but we can create it even before data loads
  // (the handle's internal hooks will handle loading state)
  const handle = useMemo(() => {
    if (!thing) return null;
    const metadata: HandleMetadata = {
      key: thing.key,
      keyJSON: thing.keyJSON,
      shortId: thing.shortId,
      thingType: thing.thingType,
      moduleId: rendererRef?.moduleId ?? '',
    };
    return createFullRendererHandle(thing.key, metadata);
  }, [thing?.key, thing?.keyJSON, thing?.shortId, thing?.thingType, rendererRef?.moduleId]);

  // Loading state
  if (loading && !thing) {
    return <div style={{ padding: 8, color: '#888', fontStyle: 'italic' }}>Loading...</div>;
  }

  // Error state
  if (error) {
    return <div style={{ padding: 8, color: '#f66' }}>{error.message}</div>;
  }

  // Not found
  if (!thing || !handle) {
    return <div style={{ padding: 8, color: '#f66' }}>Thing not found</div>;
  }

  // Render with DynamicFullRenderer
  return (
    <DynamicFullRenderer
      thingType={thing.thingType}
      rendererRef={rendererRef}
      handle={handle}
      args={args}
      isTopLevel={isTopLevel}
    />
  );
}
