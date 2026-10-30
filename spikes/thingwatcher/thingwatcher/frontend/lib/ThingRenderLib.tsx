/**
 * ThingRenderLib - Internal rendering components for thingwatcher app
 *
 * This module contains the internal implementations of:
 * - ChildNodeView: Render another thing inline with its FullRenderer
 * - NodeRef: Render a clickable reference to another thing with its RefRenderer
 * - InlineNodeRef: Render a compact inline reference
 *
 * These components are used directly by the app's internal code.
 * For dynamic renderers, use ThingWatcherBindingsExternal which wraps these
 * in ShadowSlot for proper shadow DOM integration.
 *
 * NOTE: This module re-exports handle-based prop types from handleFactory.
 * The actual handle creation happens in the parent components (LowLevelNodeView, etc.)
 */

import React, { useCallback, useMemo } from 'react';
import { Handle, useNodeId, type HandleProps } from '@xyflow/react';
import { ThingContextMenu } from '../components/ThingContextMenu';
import {
  useThingWatcher,
  useThing,
  useThingTypeConfig,
  useBreadcrumbSource,
  usePinnedThings,
  BreadcrumbSourceProvider,
  type BreadcrumbSource,
  type PendingBreadcrumb,
} from '../hooks';
import type { ThingKeyInput } from '../hooks';
import { LowLevelNodeView } from './LowLevelNodeView';
import { DynamicRefRenderer, DynamicInlineRefRenderer } from '../components/DynamicRenderer';
import type { RendererReference } from '../types';
import { HoverPreviewCard } from './HoverPreview';
import styles from './ThingRenderLib.module.css';
import { useThingUIEvents } from './ThingUIEventsContext';
import { setThingDragData, type ThingDropInfo } from './dragConstants';
import {
  createRefRendererHandle,
  type RefRendererHandle,
  type HandleMetadata,
} from './handleFactory';
import { useRegister } from './Registry';
import { useGraphRegistry, PORTS_KEY, type PortInfo } from '../components/Graph/GraphEdgeConfigContext';

// =============================================================================
// Re-export Types (handle-based)
// =============================================================================

// Re-export handle types and prop interfaces
export type {
  FullRendererHandle,
  RefRendererHandle,
  PreviewRendererHandle,
  LinkInfo,
  HandleMetadata,
} from './handleFactory';

export type {
  TriggerResult,
  TriggerRecord,
} from './rendererHooks';

// Handle-based prop types for custom renderers
export interface FullRendererProps {
  handle: import('./handleFactory').FullRendererHandle;
  args: Record<string, unknown>;
  isTopLevel: boolean;
}

export interface RefRendererProps {
  handle: RefRendererHandle;
  args: Record<string, unknown>;
}

export interface PreviewRendererProps {
  handle: import('./handleFactory').PreviewRendererHandle;
}

/**
 * Props for GraphNodeRenderer - custom node for React Flow graph visualization.
 * Uses RefRendererHandle (same hooks as RefRenderer).
 */
export interface GraphNodeRendererProps {
  handle: RefRendererHandle;
}

// =============================================================================
// ChildNodeView Component
// =============================================================================

export interface ChildNodeViewProps {
  /** Thing key as base64 string */
  thingKey?: string;
  /** Thing key as JSON value (from keyJSON field) */
  keyJSON?: unknown;
  /** Custom args to pass to the FullRenderer */
  args?: Record<string, unknown>;
}

/**
 * ChildNodeView - Renders another thing inline with its FullRenderer.
 * Use this to embed a full thing view inside another renderer.
 *
 * Always extends the breadcrumb chain:
 * - If parent has a context, adds a pending breadcrumb from parent to this child
 * - If no parent context (e.g., top-level), starts a new chain with this child as currentKey
 */
export function ChildNodeView({ thingKey, keyJSON, args = {} }: ChildNodeViewProps) {
  const parentContext = useBreadcrumbSource();

  // Resolve key from thingKey or keyJSON
  const { thing: resolvedThing } = useThing({
    keyInput: thingKey !== undefined
      ? { key: thingKey }
      : keyJSON !== undefined
        ? { keyJSON }
        : { key: '' },
    pause: thingKey === undefined && keyJSON === undefined,
    includeData: false,
  });

  // Build the child's breadcrumb context (extends parent chain or starts new)
  const childContext = useMemo((): BreadcrumbSource | null => {
    if (!resolvedThing) return null;

    if (!parentContext?.currentKey) {
      // No parent context - start a new chain (like HoverPreviewCard)
      // This allows nested nodes to still accumulate breadcrumbs
      return {
        pendingBreadcrumbs: [],
        currentKey: resolvedThing.key,
        type: 'link',
      };
    }

    // Add breadcrumb from parent to this child
    const newBreadcrumb: PendingBreadcrumb = {
      fromKey: parentContext.currentKey,
      toKey: resolvedThing.key,
      type: 'link', // ChildNodeView always creates link-type breadcrumbs
    };

    return {
      pendingBreadcrumbs: [...parentContext.pendingBreadcrumbs, newBreadcrumb],
      currentKey: resolvedThing.key,
      type: 'link',
    };
  }, [parentContext, resolvedThing?.key]);

  // Error states
  if (thingKey === undefined && keyJSON === undefined) {
    return <div className={styles.childnodeviewError}>ChildNodeView requires thingKey or keyJSON</div>;
  }

  if (!resolvedThing) {
    return <div className={styles.childnodeviewLoading}>Loading...</div>;
  }

  // Use LowLevelNodeView with isTopLevel: false
  // Always wrap with breadcrumb context
  const content = (
    <LowLevelNodeView
      thingKey={resolvedThing.key}
      isTopLevel={false}
      args={args}
    />
  );

  if (childContext) {
    return (
      <BreadcrumbSourceProvider value={childContext}>
        {content}
      </BreadcrumbSourceProvider>
    );
  }

  return content;
}

// =============================================================================
// NodeRef and InlineNodeRef Components
// =============================================================================

export interface NodeRefProps {
  /** Thing key as base64 string */
  thingKey?: string;
  /** Thing key as JSON value (from keyJSON field) */
  keyJSON?: unknown;
  /** Custom args to pass to the RefRenderer */
  args?: Record<string, unknown>;
}

/** Shared data for NodeRef and InlineNodeRef */
interface NodeRefData {
  thing: NonNullable<ReturnType<typeof useThing>['thing']>;
  rendererRef: RendererReference | null;
  handle: RefRendererHandle;
  args: Record<string, unknown>;
  handleClick: (e: React.MouseEvent) => void;
  handleDragStart: (e: React.DragEvent) => void;
}

type NodeRefDataResult =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'missing'; keyDisplay: string }
  | { status: 'ready'; data: NodeRefData };

/**
 * Private hook that fetches thing data and creates handle for NodeRef/InlineNodeRef.
 */
function useNodeRefData(
  thingKey: string | undefined,
  keyJSON: unknown | undefined,
  args: Record<string, unknown>
): NodeRefDataResult {
  const { onClickThing } = useThingUIEvents();
  const bcSource = useBreadcrumbSource();
  const { addBreadcrumbs } = usePinnedThings();

  // Build key input
  const keyInput: ThingKeyInput | null = useMemo(() => {
    if (thingKey !== undefined) {
      return { key: thingKey };
    } else if (keyJSON !== undefined) {
      return { keyJSON };
    }
    return null;
  }, [thingKey, keyJSON]);

  // Fetch thing with data
  const { thing, loading, error } = useThing({
    keyInput: keyInput ?? { key: '' },
    pause: keyInput === null,
    includeData: true,
  });

  // Get renderer config for this thing type
  const { config: typeConfig } = useThingTypeConfig(thing?.thingType ?? '');
  const rendererRef = typeConfig?.renderer ?? null;

  // Memoize callbacks - must be called before early returns (hooks rules)
  const handleClick = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (thing?.key) {
      // Always create breadcrumbs if we have a source context with a currentKey
      // Self-reference filtering is handled by addBreadcrumbs
      if (bcSource?.currentKey) {
        // Create the final breadcrumb
        const finalBreadcrumb: PendingBreadcrumb = {
          fromKey: bcSource.currentKey,
          toKey: thing.key,
          type: bcSource.type,
        };

        // Create all breadcrumbs (pending chain + final)
        // addBreadcrumbs will skip self-references and duplicates
        addBreadcrumbs([...bcSource.pendingBreadcrumbs, finalBreadcrumb]);
      }

      onClickThing(thing.key);
    }
  }, [onClickThing, thing?.key, bcSource, addBreadcrumbs]);

  const handleDragStart = useCallback((e: React.DragEvent) => {
    if (thing) {
      // Build the breadcrumb chain for this drag
      // Always include pending breadcrumbs even for self-reference
      // Self-reference filtering is handled by addBreadcrumbs when dropped
      let pendingBreadcrumbs: PendingBreadcrumb[] = bcSource?.pendingBreadcrumbs ?? [];
      if (bcSource?.currentKey) {
        const finalBreadcrumb: PendingBreadcrumb = {
          fromKey: bcSource.currentKey,
          toKey: thing.key,
          type: bcSource.type,
        };
        pendingBreadcrumbs = [...pendingBreadcrumbs, finalBreadcrumb];
      }

      const dropInfo: ThingDropInfo = {
        key: thing.key,
        keyJSON: thing.keyJSON,
        shortId: thing.shortId,
        thingType: thing.thingType,
        pendingBreadcrumbs,
      };
      setThingDragData(e.dataTransfer, dropInfo);
    }
  }, [thing?.key, thing?.keyJSON, thing?.shortId, thing?.thingType, bcSource]);

  // Create handle - must be after other hooks but before early returns
  // The handle is created fresh each render, but the internal hook methods
  // will be called by the renderer and will use React's hook state
  const handle = useMemo(() => {
    if (!thing) return null;
    const metadata: HandleMetadata = {
      key: thing.key,
      keyJSON: thing.keyJSON,
      shortId: thing.shortId,
      thingType: thing.thingType,
      moduleId: rendererRef?.moduleId ?? '',
    };
    return createRefRendererHandle(thing.key, metadata);
  }, [thing?.key, thing?.keyJSON, thing?.shortId, thing?.thingType, rendererRef?.moduleId]);

  // Compute key display string for "not found" cases
  const keyDisplay = useMemo(() => {
    if (keyJSON !== undefined) {
      return JSON.stringify(keyJSON);
    }
    if (thingKey !== undefined) {
      // Try to show a shortened version of the base64 key
      return thingKey.length > 20 ? `${thingKey.slice(0, 20)}...` : thingKey;
    }
    return '(unknown)';
  }, [thingKey, keyJSON]);

  // Handle non-ready states (after all hooks have been called)
  if (keyInput === null) {
    return { status: 'missing', keyDisplay };
  }
  if (loading && !thing) {
    return { status: 'loading' };
  }
  if (error) {
    return { status: 'error' };
  }
  if (!thing || !handle) {
    return { status: 'missing', keyDisplay };
  }

  return {
    status: 'ready',
    data: {
      thing,
      rendererRef,
      handle,
      args,
      handleClick,
      handleDragStart,
    },
  };
}

/**
 * NodeRef - Renders a clickable block reference to another thing using its RefRenderer.
 * Block-level element that fills its container width.
 * The entire ref is draggable. Click to navigate, right-click for context menu.
 * Hovering shows a preview card.
 */
export function NodeRef({ thingKey, keyJSON, args = {} }: NodeRefProps) {
  const result = useNodeRefData(thingKey, keyJSON, args);

  if (result.status === 'loading') {
    return <div className={`${styles.noderef} ${styles.noderefLoading}`}>...</div>;
  }
  if (result.status === 'error') {
    return <div className={`${styles.noderef} ${styles.noderefError}`}>!</div>;
  }
  if (result.status === 'missing') {
    return (
      <div className={`${styles.noderef} ${styles.noderefMissing}`}>
        Not found: {result.keyDisplay}
      </div>
    );
  }

  const { thing, rendererRef, handle, args: rendererArgs, handleClick, handleDragStart } = result.data;

  return (
    <HoverPreviewCard thingKey={thing.key}>
      <ThingContextMenu thingKey={thing.key}>
        <div
          className={styles.noderef}
          onClick={handleClick}
          draggable
          onDragStart={handleDragStart}
          title="Click to select, right-click for menu, drag to open in panel"
          style={{ maxWidth: '100%' }}
        >
          <DynamicRefRenderer
            thingType={thing.thingType}
            rendererRef={rendererRef}
            handle={handle}
            args={rendererArgs}
          />
        </div>
      </ThingContextMenu>
    </HoverPreviewCard>
  );
}

/**
 * InlineNodeRef - Renders a clickable inline reference to another thing using its InlineRefRenderer.
 * Inline element that flows with surrounding text, as compact as possible.
 * The entire ref is draggable. Click to navigate, right-click for context menu.
 * Hovering shows a preview card.
 */
export function InlineNodeRef({ thingKey, keyJSON, args = {} }: NodeRefProps) {
  const result = useNodeRefData(thingKey, keyJSON, args);

  if (result.status === 'loading') {
    return <span className={`${styles.inlineNoderef} ${styles.noderefLoading}`}>...</span>;
  }
  if (result.status === 'error') {
    return <span className={`${styles.inlineNoderef} ${styles.noderefError}`}>!</span>;
  }
  if (result.status === 'missing') {
    return (
      <span className={`${styles.inlineNoderef} ${styles.noderefMissing}`}>
        Not found: {result.keyDisplay}
      </span>
    );
  }

  const { thing, rendererRef, handle, args: rendererArgs, handleClick, handleDragStart } = result.data;

  return (
    <HoverPreviewCard thingKey={thing.key}>
      <ThingContextMenu thingKey={thing.key}>
        <span
          className={styles.inlineNoderef}
          onClick={handleClick}
          draggable
          onDragStart={handleDragStart}
          title="Click to select, right-click for menu, drag to open in panel"
        >
          <DynamicInlineRefRenderer
            thingType={thing.thingType}
            rendererRef={rendererRef}
            handle={handle}
            args={rendererArgs}
          />
        </span>
      </ThingContextMenu>
    </HoverPreviewCard>
  );
}

// =============================================================================
// Port Component (for graph node renderers)
// =============================================================================

// Re-export Position as PortPosition for external use
export { Position as PortPosition } from '@xyflow/react';

export type PortProps = HandleProps;

/**
 * Port - A connection point on a graph node.
 *
 * Wraps React Flow's Handle and registers the port with the graph registry.
 * If used outside a graph context (e.g., in tests), it just renders the Handle
 * without registration.
 *
 * NOTE: This is the internal version. Dynamic renderers should import Port from
 * 'thingwatcher-bindings' which wraps this in ShadowSlot for light DOM rendering.
 *
 * ## Styling Note (for wrapped version)
 *
 * When used via 'thingwatcher-bindings', Port renders in light DOM (outside the
 * shadow root). This means CSS styles from adoptedStyleSheets will NOT apply.
 * Use inline `style` props to style ports instead of CSS classes.
 */
export function Port(props: PortProps) {
  const nodeId = useNodeId();
  const registry = useGraphRegistry();
  const portId = props.id ?? (props.type === 'source' ? '__default_source__' : '__default_target__');

  // Memoize the port info to avoid re-registration on every render
  const portInfo = useMemo<PortInfo | null>(
    () => (nodeId ? { nodeId, portId, type: props.type } : null),
    [nodeId, portId, props.type]
  );

  useRegister(registry, PORTS_KEY, portInfo);

  return <Handle {...props} />;
}
