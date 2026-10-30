/**
 * Type definitions for thingwatcher-bindings
 *
 * This module is available to custom renderers at runtime via esbuild's
 * virtual module system. This file provides TypeScript types for
 * development-time typechecking (e.g., with Deno).
 *
 * Custom renderers import from 'thingwatcher-bindings' to access:
 * - Hooks: usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable, useTrigger, useDraggableIdMarker, usePersistentThingState
 * - Components: ChildNodeView, NodeRef, InlineNodeRef, AdoptStyleSheet
 * - Graph Components: Port, PortPosition, LinkEdgeConfig (for custom graph node renderers)
 * - Types: ThingWatcherRenderConfig, RendererSet, GraphNodeRendererProps (for typing the default export)
 *
 * All renderer modules must have a default export of type ThingWatcherRenderConfig:
 *
 * @example
 * ```tsx
 * import type { ThingWatcherRenderConfig, FullRendererProps } from 'thingwatcher-bindings';
 *
 * function MyFullRenderer({ handle }: FullRendererProps) { ... }
 * function MyRefRenderer({ handle }: RefRendererProps) { ... }
 *
 * export default {
 *   dialect: "0.1",
 *   renderers: {
 *     "MyType": { full: MyFullRenderer, ref: MyRefRenderer },
 *   }
 * } as ThingWatcherRenderConfig;
 * ```
 */

import type { FC, ReactNode, CSSProperties } from "react";

// =============================================================================
// Handle Types (opaque, branded)
// =============================================================================

declare const fullRendererBrand: unique symbol;
declare const refRendererBrand: unique symbol;
declare const previewRendererBrand: unique symbol;

/**
 * Opaque handle for FullRenderer.
 * Supports all hooks: usePayload, useDataChunks, useLinks, useThingMetadata,
 * useTriggerable, useTrigger, useDraggableIdMarker, usePersistentThingState
 */
export interface FullRendererHandle {
  readonly [fullRendererBrand]: true;
}

/**
 * Opaque handle for RefRenderer and InlineRefRenderer.
 * Supports: usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable
 */
export interface RefRendererHandle {
  readonly [refRendererBrand]: true;
}

/**
 * Opaque handle for PreviewRenderer.
 * Supports: usePayload, useDataChunks, useLinks, useThingMetadata, useDraggableIdMarker
 */
export interface PreviewRendererHandle {
  readonly [previewRendererBrand]: true;
}

// =============================================================================
// Data Types
// =============================================================================

/** Link info returned by useLinks */
export interface LinkInfo {
  linkType: string;
  linkKey: string;
  linkKeyJSON: unknown;
  toThingKey: string;
  toThingKeyJSON: unknown;
  toThingShortId: number;
  toThingType: string;
}

/** Result of triggering a thing */
export interface TriggerResult {
  ok: boolean;
  error?: string;
}

/**
 * Trigger record returned by useTriggerable.
 * Contains only the essential info - the thing identity is already known from the handle.
 */
export interface TriggerRecord {
  triggerTick: number;
  payload: unknown;
}

/** Metadata returned by useThingMetadata */
export interface ThingMetadata {
  key: string;
  keyJSON: unknown;
  shortId: number;
  thingType: string;
}

// =============================================================================
// Renderer Props (handle-based)
// =============================================================================

/**
 * Props for FullRenderer - the main card/detail view of a thing.
 * Custom renderers export this as: export function FullRenderer(props: FullRendererProps) { ... }
 */
export interface FullRendererProps {
  /** Opaque handle - use hooks to access data */
  handle: FullRendererHandle;
  /** Custom args from parent (e.g., isOwned, custom display options) */
  args: Record<string, unknown>;
  /** True if this is a top-level panel view, false if nested in another renderer */
  isTopLevel: boolean;
}

/**
 * Props for RefRenderer - a compact reference/link view of a thing.
 * Custom renderers export this as: export function RefRenderer(props: RefRendererProps) { ... }
 */
export interface RefRendererProps {
  /** Opaque handle - use hooks to access data */
  handle: RefRendererHandle;
  /** Custom args from parent */
  args: Record<string, unknown>;
}

/**
 * Props for PreviewRenderer - a compact preview for hover cards.
 * Custom renderers export this as: export function PreviewRenderer(props: PreviewRendererProps) { ... }
 *
 * PreviewRenderer is read-only (no trigger) and doesn't receive args.
 * It can use NodeRef/InlineNodeRef but should NOT use ChildNodeView (convention).
 */
export interface PreviewRendererProps {
  /** Opaque handle - use hooks to access data */
  handle: PreviewRendererHandle;
}

/**
 * Props for GraphNodeRenderer - a custom node for React Flow graph visualization.
 * Custom renderers export this as: export function GraphNodeRenderer(props: GraphNodeRendererProps) { ... }
 *
 * Graph nodes use RefRendererHandle (same hooks as RefRenderer).
 * They can use Port components to define connection points and LinkEdgeConfig
 * to customize outgoing edge appearance.
 *
 * @example
 * ```tsx
 * function MyGraphNode({ handle }: GraphNodeRendererProps) {
 *   const payload = usePayload(handle);
 *   const links = useLinks(handle);
 *   const { shortId } = useThingMetadata(handle);
 *
 *   return (
 *     <div style={{ padding: 8, background: '#1a1a2e', borderRadius: 4 }}>
 *       <Port type="target" position={PortPosition.Top} id="in" />
 *       <span>#{shortId} {payload?.name}</span>
 *       <Port type="source" position={PortPosition.Bottom} id="out" />
 *
 *       {links.map(link => (
 *         <LinkEdgeConfig
 *           key={link.linkKey}
 *           linkKey={link.linkKey}
 *           sourcePort="out"
 *           targetPort="in"
 *         />
 *       ))}
 *     </div>
 *   );
 * }
 * ```
 */
export interface GraphNodeRendererProps {
  /** Opaque handle - use hooks to access data (same as RefRenderer) */
  handle: RefRendererHandle;
}

// =============================================================================
// Hooks
// =============================================================================

/**
 * Get the thing's payload data.
 *
 * @param handle - Renderer handle
 * @param options - Optional: { chunk: 'first' | 'latest' }, defaults to 'first'
 * @returns The payload data (first or latest chunk)
 *
 * @example
 * ```tsx
 * const data = usePayload<MyDataType>(handle);
 * const latestData = usePayload<MyDataType>(handle, { chunk: 'latest' });
 * ```
 */
export declare function usePayload<T = unknown>(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle,
  options?: { chunk?: "first" | "latest" }
): T;

/**
 * Get all data chunks for the thing.
 *
 * @param handle - Renderer handle
 * @returns Array of chunk data (oldest first)
 *
 * @example
 * ```tsx
 * const chunks = useDataChunks<LogEntry>(handle);
 * chunks.forEach(entry => console.log(entry));
 * ```
 */
export declare function useDataChunks<T = unknown>(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle
): T[];

/**
 * Get links from this thing to others.
 *
 * @param handle - Renderer handle
 * @returns Array of link info
 *
 * @example
 * ```tsx
 * const links = useLinks(handle);
 * const childLinks = links.filter(l => l.linkType === 'child');
 * ```
 */
export declare function useLinks(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle
): LinkInfo[];

/**
 * Get thing metadata (key, shortId, thingType).
 *
 * @param handle - Renderer handle
 * @returns Metadata object
 *
 * @example
 * ```tsx
 * const { shortId, thingType, keyJSON } = useThingMetadata(handle);
 * ```
 */
export declare function useThingMetadata<K = unknown>(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle
): ThingMetadata & { keyJSON: K };

/**
 * Get triggerable status and trigger record at current tick.
 * Available for FullRenderer and RefRenderer.
 *
 * @param handle - Renderer handle (Full or Ref)
 * @returns [triggerable, triggerRecord] tuple
 *
 * @example
 * ```tsx
 * const [triggerable, triggerRecord] = useTriggerable(handle);
 * if (triggerRecord) {
 *   console.log('Was triggered with:', triggerRecord.payload);
 * }
 * ```
 */
export declare function useTriggerable(
  handle: FullRendererHandle | RefRendererHandle
): [triggerable: boolean, triggerRecord: TriggerRecord | null];

/**
 * Get the trigger function. FullRenderer only.
 *
 * @param handle - FullRendererHandle
 * @returns Trigger function that accepts a payload
 *
 * @example
 * ```tsx
 * const trigger = useTrigger<{ action: string }>(handle);
 * // ...
 * <button onClick={() => trigger({ action: 'go' })}>Go</button>
 * ```
 */
export declare function useTrigger<T = unknown>(
  handle: FullRendererHandle
): (payload: T) => Promise<TriggerResult>;

/**
 * Get a component to wrap the draggable ID element.
 * Available for FullRenderer and PreviewRenderer.
 *
 * @param handle - Renderer handle (Full or Preview)
 * @returns Component to wrap draggable content
 *
 * @example
 * ```tsx
 * const DraggableIDMarker = useDraggableIdMarker(handle);
 * return (
 *   <DraggableIDMarker>
 *     <span style={{ cursor: 'grab' }}>Drag me</span>
 *   </DraggableIDMarker>
 * );
 * ```
 */
export declare function useDraggableIdMarker(
  handle: FullRendererHandle | PreviewRendererHandle
): FC<{ children: ReactNode }>;

/**
 * Hook for storing state that persists across component re-mounts.
 * FullRenderer only.
 *
 * State is keyed by the thing's shortId + the user-provided key,
 * so different things have separate state, but the same thing
 * opened in multiple panels shares state.
 *
 * @param handle - FullRendererHandle
 * @param key - User-provided key to namespace the state
 * @returns Tuple of [value, setValue] where value starts as undefined.
 *          setValue accepts either a direct value or an updater function (prev => next).
 *
 * @example
 * ```tsx
 * const [expanded, setExpanded] = usePersistentThingState<boolean>(handle, "expanded");
 * const [count, setCount] = usePersistentThingState<number>(handle, "count");
 *
 * // Set directly
 * setExpanded(true);
 *
 * // Use updater function
 * setCount(prev => (prev ?? 0) + 1);
 *
 * // Clear state by setting to undefined
 * setExpanded(undefined);
 * ```
 */
export declare function usePersistentThingState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined | ((prev: T | undefined) => T | undefined)) => void];

/**
 * Hook for storing state that persists across component re-mounts and is
 * shared across all things rendered by the same module.
 * FullRenderer only.
 *
 * State is keyed by the module's ID + the user-provided key,
 * so all things rendered by the same .tsx module share this state.
 * Useful for shared resources like styling observers, caches, etc.
 *
 * @param handle - FullRendererHandle
 * @param key - User-provided key to namespace the state
 * @returns Tuple of [value, setValue] where value starts as undefined.
 *          setValue accepts either a direct value or an updater function (prev => next).
 *
 * @example
 * ```tsx
 * // Initialize shared resource on first use
 * const [sharedState, setSharedState] = usePersistentGlobalState<MyState>(handle, "shared");
 *
 * if (!sharedState) {
 *   setSharedState(prev => {
 *     if (prev) return prev; // Race condition guard
 *     return createExpensiveResource();
 *   });
 * }
 * ```
 */
export declare function usePersistentGlobalState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined | ((prev: T | undefined) => T | undefined)) => void];

// =============================================================================
// Drop Region Types and Hook
// =============================================================================

/**
 * Metadata about a dragged/dropped thing.
 */
export interface ThingDropInfo {
  /** Base64-encoded CBOR key */
  key: string;
  /** JSON-deserialized key, or null if not JSON-serializable */
  keyJSON: unknown;
  /** Short numeric ID for display */
  shortId: number;
  /** Thing type string */
  thingType: string;
}

/**
 * Props for ThingDropRegion component.
 */
export interface ThingDropRegionProps {
  children: ReactNode;
  /** Optional className for the wrapper div */
  className?: string;
  /** Optional style for the wrapper div */
  style?: CSSProperties;
}

/**
 * Result of useThingDropRegion hook.
 */
export interface UseThingDropRegionResult {
  /**
   * Component that wraps children and suppresses dockview drop handling within its bounds.
   * Does NOT handle the actual drop - use standard onDragOver/onDrop on inner elements.
   * Accepts an optional `style` prop for the wrapper div.
   */
  ThingDropRegion: FC<ThingDropRegionProps>;

  /** Mime type that matches any thing (use with dataTransfer.types.includes()) */
  mimeTypeForAnyThing: string;

  /** Returns mime type for things of a specific type */
  mimeTypeForThingType: (thingType: string) => string;

  /** Returns mime type for a specific thing key */
  mimeTypeForSpecificThing: (thingKey: string) => string;

  /** Extract dropped thing metadata from DataTransfer, or null if not a thing */
  extractDroppedThing: (dataTransfer: DataTransfer) => ThingDropInfo | null;
}

/**
 * Hook for creating drop zones in custom renderers.
 * FullRenderer only.
 *
 * Returns a ThingDropRegion component that suppresses dockview drop handling,
 * along with helper functions for checking and extracting dropped things.
 *
 * The ThingDropRegion component only suppresses dockview - the renderer must
 * handle actual drop events using standard HTML5 drag/drop APIs.
 *
 * @param handle - FullRendererHandle
 * @returns Object with ThingDropRegion component and helper functions
 *
 * @example
 * ```tsx
 * function MyDropZone({ handle }: FullRendererProps) {
 *   const {
 *     ThingDropRegion,
 *     mimeTypeForAnyThing,
 *     mimeTypeForThingType,
 *     extractDroppedThing,
 *   } = useThingDropRegion(handle);
 *
 *   const [isDragOver, setIsDragOver] = useState(false);
 *   const [dropped, setDropped] = usePersistentThingState<ThingDropInfo>(handle, 'dropped');
 *
 *   return (
 *     <ThingDropRegion>
 *       <div
 *         style={{ border: isDragOver ? '2px dashed #4a9' : '2px dashed #333', padding: 16 }}
 *         onDragOver={(e) => {
 *           // Accept any thing, or filter by type:
 *           // e.dataTransfer.types.includes(mimeTypeForThingType('Apple'))
 *           if (e.dataTransfer.types.includes(mimeTypeForAnyThing)) {
 *             e.preventDefault();
 *             setIsDragOver(true);
 *           }
 *         }}
 *         onDragLeave={() => setIsDragOver(false)}
 *         onDrop={(e) => {
 *           e.preventDefault();
 *           setIsDragOver(false);
 *           const info = extractDroppedThing(e.dataTransfer);
 *           if (info) setDropped(info);
 *         }}
 *       >
 *         {dropped ? <NodeRef thingKey={dropped.key} /> : 'Drop a thing here'}
 *       </div>
 *     </ThingDropRegion>
 *   );
 * }
 * ```
 */
export declare function useThingDropRegion(
  handle: FullRendererHandle
): UseThingDropRegionResult;

// =============================================================================
// Components (declare-only, runtime provides these)
// =============================================================================

export interface ChildNodeViewProps {
  /** Thing key as base64 string */
  thingKey?: string;
  /** Thing key as JSON value (from keyJSON field) */
  keyJSON?: unknown;
  /** Custom args to pass to the FullRenderer */
  args?: Record<string, unknown>;
}

export interface NodeRefProps {
  /** Thing key as base64 string */
  thingKey?: string;
  /** Thing key as JSON value (from keyJSON field) */
  keyJSON?: unknown;
  /** Custom args to pass to the RefRenderer */
  args?: Record<string, unknown>;
}

/**
 * ChildNodeView - Renders another thing inline with its FullRenderer.
 * Use this to embed a full thing view inside another renderer.
 */
export declare const ChildNodeView: FC<ChildNodeViewProps>;

/**
 * NodeRef - Renders a clickable block reference to another thing using its RefRenderer.
 * Block-level element that fills its container width.
 * The entire ref is draggable. Click to navigate, right-click for context menu.
 * Hovering shows a preview card.
 */
export declare const NodeRef: FC<NodeRefProps>;

/**
 * InlineNodeRef - Renders a clickable inline reference to another thing using its InlineRefRenderer.
 * Inline element that flows with surrounding text, as compact as possible.
 * The entire ref is draggable. Click to navigate, right-click for context menu.
 * Hovering shows a preview card.
 */
export declare const InlineNodeRef: FC<NodeRefProps>;

export interface AdoptStyleSheetProps {
  /** The CSSStyleSheet to adopt into the shadow root */
  sheet: CSSStyleSheet;
}

/**
 * AdoptStyleSheet - Declaratively adopts a stylesheet into the containing shadow root.
 *
 * Use this inside a custom renderer to attach additional dynamically-constructed stylesheets
 * (e.g., from twind) to the shadow root where the renderer content is embedded, so that the styles
 * defined in it will apply to the renderer content. (Note that static stylesheets can instead be
 * added by directly importing them via esbuild's CSS loader support; stylesheets bundled this way
 * will be adopted automatically.)
 *
 * Renders nothing - just registers the sheet with the parent shadow root.
 *
 * @example
 * ```tsx
 * const [twindState] = usePersistentGlobalState<TwindState>(handle, 'twind');
 * // ... set up twind state ...
 * return (
 *   <>
 *     {twindState && <AdoptStyleSheet sheet={twindState.sheet} />}
 *     <div className="p-4 bg-slate-800">...</div>
 *   </>
 * );
 * ```
 */
export declare const AdoptStyleSheet: FC<AdoptStyleSheetProps>;

// =============================================================================
// Graph Components (for custom graph node renderers)
// =============================================================================

/**
 * Position enum for Port placement.
 * Re-exported from React Flow's Position enum.
 */
export declare enum PortPosition {
  Left = "left",
  Top = "top",
  Right = "right",
  Bottom = "bottom",
}

/**
 * Props for the Port component.
 * Based on React Flow's HandleProps.
 */
export interface PortProps {
  /** Whether this is a source (outgoing) or target (incoming) port */
  type: "source" | "target";
  /** Position of the port on the node */
  position: PortPosition;
  /** Unique ID for this port within the node (used for edge routing) */
  id?: string;
  /** Whether the port is connectable (default: false for view-only graphs) */
  isConnectable?: boolean;
  /** Additional CSS class name */
  className?: string;
  /** Inline styles */
  style?: React.CSSProperties;
}

/**
 * Port - A connection point on a graph node for edge routing.
 *
 * This is a wrapper around React Flow's Handle component that registers
 * the port with the graph context for edge configuration.
 *
 * Use inside a GraphNodeRenderer to define where edges can connect.
 *
 * **Styling Note:** Port renders in light DOM (outside the shadow root) so that
 * React Flow can find it for edge routing. This means CSS styles from the
 * shadow DOM's adoptedStyleSheets will NOT apply to Port. Use inline `style`
 * props to style ports instead of CSS classes.
 *
 * @example
 * ```tsx
 * <div className="my-node">
 *   <Port type="target" position={PortPosition.Top} id="in" style={{ background: '#4ecdc4' }} />
 *   <span>Node content</span>
 *   <Port type="source" position={PortPosition.Bottom} id="out" style={{ background: '#4ecdc4' }} />
 * </div>
 * ```
 */
export declare const Port: FC<PortProps>;

/**
 * Props for the LinkEdgeConfig component.
 */
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
 * LinkEdgeConfig - Declaratively configure an outgoing edge from this node.
 *
 * Renders nothing visible - just registers edge configuration with the graph.
 * Use inside a GraphNodeRenderer to customize how edges from this node appear.
 *
 * @example
 * ```tsx
 * function MyGraphNode({ handle }: GraphNodeRendererProps) {
 *   const links = useLinks(handle);
 *
 *   return (
 *     <div>
 *       <Port type="source" position={PortPosition.Bottom} id="out" />
 *
 *       {links.map(link => (
 *         <LinkEdgeConfig
 *           key={link.linkKey}
 *           linkKey={link.linkKey}
 *           sourcePort="out"
 *           targetPort="in"
 *           label={link.linkType}
 *           style={{ stroke: '#4ecdc4' }}
 *         />
 *       ))}
 *     </div>
 *   );
 * }
 * ```
 */
export declare const LinkEdgeConfig: FC<LinkEdgeConfigProps>;

// =============================================================================
// Renderer Module Config Types
// =============================================================================

/**
 * A set of renderer components for a single thing type within a module.
 * All fields are optional - missing renderers will use fallback implementations.
 */
export interface RendererSet {
  /** Full card/detail view renderer */
  full?: FC<FullRendererProps>;
  /** Block-level reference renderer */
  ref?: FC<RefRendererProps>;
  /** Inline reference renderer */
  inline?: FC<RefRendererProps>;
  /** Hover preview renderer */
  preview?: FC<PreviewRendererProps>;
  /** Graph node renderer for React Flow visualization */
  graph?: FC<GraphNodeRendererProps>;
}

/**
 * The required default export format for all renderer modules.
 *
 * A single module can export renderers for multiple thing types.
 * Each thing type maps to a RendererSet containing the component implementations.
 *
 * @example
 * ```tsx
 * // Single type
 * export default {
 *   dialect: "0.1",
 *   renderers: {
 *     "Widget": { full: WidgetFull, ref: WidgetRef },
 *   }
 * } as ThingWatcherRenderConfig;
 *
 * // Multiple types in one module
 * export default {
 *   dialect: "0.1",
 *   renderers: {
 *     "Container": { full: ContainerFull, ref: ContainerRef, preview: ContainerPreview },
 *     "Item": { full: ItemFull, ref: ItemRef },
 *   }
 * } as ThingWatcherRenderConfig;
 * ```
 */
export interface ThingWatcherRenderConfig {
  /** Dialect version - must be "0.1" */
  dialect: "0.1";
  /** Map from renderer key to renderer set */
  renderers: Record<string, RendererSet>;
}
