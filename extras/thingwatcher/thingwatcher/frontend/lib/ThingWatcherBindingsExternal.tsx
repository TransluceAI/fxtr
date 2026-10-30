/**
 * ThingWatcherBindingsExternal - SDK for custom thing renderers (shadow DOM version)
 *
 * This module is injected into dynamically compiled renderers as 'thingwatcher-bindings'.
 * Custom renderers import from 'thingwatcher-bindings' to access:
 *
 * Hooks:
 * - usePayload: Get thing's payload data
 * - useDataChunks: Get all data chunks
 * - useLinks: Get links from this thing
 * - useThingMetadata: Get thing metadata (key, shortId, thingType)
 * - useTriggerable: Get triggerable status and trigger record
 * - useTrigger: Get trigger function (FullRenderer only)
 * - useDraggableIdMarker: Get draggable marker component
 * - usePersistentThingState: Store per-thing persistent state (FullRenderer only)
 * - usePersistentGlobalState: Store module-scoped persistent state (FullRenderer only)
 * - useThingDropRegion: Create drop zones that accept dragged things (FullRenderer only)
 *
 * Components:
 * - ChildNodeView: Render another thing inline with its FullRenderer
 * - NodeRef: Render a clickable block reference to another thing
 * - InlineNodeRef: Render a compact inline reference
 * - AdoptStyleSheet: Declaratively adopt a stylesheet into the shadow root
 *
 * Graph Components (for custom graph node renderers):
 * - Port: Connection point on a graph node (wrapper around React Flow Handle)
 * - PortPosition: Enum for port positions (Top, Bottom, Left, Right)
 * - LinkEdgeConfig: Declarative edge configuration
 *
 * All components except AdoptStyleSheet are wrapped in ShadowSlot so they render
 * in light DOM when used inside a shadow DOM context. This allows them to:
 * - Inherit app styles
 * - Participate in app context (selection, events)
 * - Show hover preview cards
 *
 * IMPORTANT: This module is ONLY for dynamic renderers. App code should import
 * directly from ThingRenderLib for the unwrapped versions.
 */

import React from 'react';
import { ShadowSlot, AdoptStyleSheet } from './ShadowDOM';
import {
  ChildNodeView as ChildNodeViewInternal,
  NodeRef as NodeRefInternal,
  InlineNodeRef as InlineNodeRefInternal,
  Port as PortInternal,
  PortPosition,
  type ChildNodeViewProps,
  type NodeRefProps,
  type PortProps,
} from './ThingRenderLib';

// Re-export hooks from rendererHooks
export {
  usePayload,
  useDataChunks,
  useLinks,
  useThingMetadata,
  useTriggerable,
  useTrigger,
  useDraggableIdMarker,
  usePersistentThingState,
  usePersistentGlobalState,
} from './rendererHooks';

// Re-export useThingDropRegion hook and types
export { useThingDropRegion, type UseThingDropRegionResult, type ThingDropInfo, type ThingDropRegionProps } from './thingDropRegion';

// Re-export types
export type {
  FullRendererHandle,
  RefRendererHandle,
  PreviewRendererHandle,
  LinkInfo,
  TriggerResult,
  TriggerRecord,
} from './rendererHooks';

// Re-export prop types from ThingRenderLib (new handle-based versions)
export type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  GraphNodeRendererProps,
  ChildNodeViewProps,
  NodeRefProps,
} from './ThingRenderLib';

// =============================================================================
// Wrapped Components
// =============================================================================

/**
 * ChildNodeView - Renders another thing inline with its FullRenderer.
 *
 * Wrapped in ShadowSlot so it renders in light DOM when inside shadow DOM.
 * This allows the child thing to have its own shadow DOM for style isolation.
 */
export function ChildNodeView(props: ChildNodeViewProps) {
  return (
    <ShadowSlot>
      <ChildNodeViewInternal {...props} />
    </ShadowSlot>
  );
}

/**
 * NodeRef - Renders a clickable block reference to another thing.
 *
 * Wrapped in ShadowSlot so it renders in light DOM when inside shadow DOM.
 * This allows it to inherit app styles and show hover preview cards.
 */
export function NodeRef(props: NodeRefProps) {
  return (
    <ShadowSlot>
      <NodeRefInternal {...props} />
    </ShadowSlot>
  );
}

/**
 * InlineNodeRef - Renders a clickable inline reference to another thing.
 *
 * Wrapped in ShadowSlot so it renders in light DOM when inside shadow DOM.
 * This allows it to inherit app styles and show hover preview cards.
 */
export function InlineNodeRef(props: NodeRefProps) {
  return (
    <ShadowSlot>
      <InlineNodeRefInternal {...props} />
    </ShadowSlot>
  );
}

// =============================================================================
// Re-exported Components (no wrapping needed)
// =============================================================================

// Re-export AdoptStyleSheet directly - it renders nothing, just uses context
export { AdoptStyleSheet, type AdoptStyleSheetProps } from './ShadowDOM';

// =============================================================================
// Graph Components (for custom graph node renderers)
// =============================================================================

// Re-export PortPosition enum
export { PortPosition };

// Re-export PortProps type
export type { PortProps };

/**
 * Port - A connection point on a graph node.
 *
 * Wrapped in ShadowSlot so it renders in light DOM when inside shadow DOM.
 * This is necessary because React Flow uses DOM queries to find Handle elements,
 * which don't work across shadow DOM boundaries.
 *
 * **Important:** Because Port renders in light DOM, CSS styles from the shadow
 * DOM's adoptedStyleSheets will NOT apply. Use inline `style` props to style
 * ports instead of CSS classes.
 */
export function Port(props: PortProps) {
  return (
    <ShadowSlot>
      <PortInternal {...props} />
    </ShadowSlot>
  );
}

// Re-export LinkEdgeConfig for declarative edge configuration
export { LinkEdgeConfig, type LinkEdgeConfigProps } from './LinkEdgeConfig';
