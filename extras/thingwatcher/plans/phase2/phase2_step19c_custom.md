# Phase 2 Step 19C: Custom Graph Renderers

Add the `graph` renderer type to the renderer system, allowing thing types to define custom graph nodes with ports and declarative edge configuration.

## Goal

Enable custom renderers for graph nodes:
- Custom graph node renderers with `<Port>` components for connection points
- Declarative `<LinkEdgeConfig>` component for customizing outgoing edges
- Node size detection and propagation to cola layout

**Deferred to Phase D:**
- Alt+drag for drag-to-panel (HTML drag API conflicts with React Flow's mouse-based dragging)
- Hover preview cards on graph nodes

## Design Decisions

### Handle Type

Graph node renderers receive a `RefRendererHandle` (same as ref renderers). This provides:
- `usePayload`, `useDataChunks`, `useLinks`, `useThingMetadata`, `useTriggerable`

Note: `useDraggableIdMarker` is NOT available in graph nodes. Instead, the entire node is draggable with special behavior (see below).

### Graph Node Interaction

Graph nodes support these interactions in Phase C:

- **Normal drag** = move node in graph (React Flow default)
- **Click** = select node (existing behavior)

**Deferred to Phase D:**
- **Option/Alt + drag** = drag to panel (like DraggableID/NodeRef)
- **Hover** = shows preview card (like NodeRef/InlineNodeRef)

This means the entire graph node is interactive - no need for a separate DraggableIDMarker inside.

### Port vs Handle Naming

React Flow calls connection points "handles", but we use "handle" for renderer handles. To avoid confusion:
- We export React Flow's `Handle` component as **`Port`** from thingwatcher-bindings
- We export `Position` enum as **`PortPosition`**
- Documentation clearly explains: "Port = React Flow Handle (connection point)"

### Declarative Edge Configuration

Instead of imperative `getSourcePort`/`getTargetPort` functions, graph nodes declaratively render `<LinkEdgeConfig>` components for their outgoing edges:

```tsx
function MyGraphNode({ handle }: GraphNodeRendererProps) {
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);

  return (
    <div>
      <Port type="target" position={PortPosition.Top} id="in" />
      <span>#{shortId}</span>
      <Port type="source" position={PortPosition.Bottom} id="out" />

      {links.map(link => (
        <LinkEdgeConfig
          key={link.linkKey}
          linkKey={link.linkKey}
          sourcePort="out"
          targetPort="in"
          label={link.linkType}
        />
      ))}
    </div>
  );
}
```

Note: No `useDraggableIdMarker` in graph renderers - graph nodes are dragged as whole units via React Flow.

`LinkEdgeConfig` doesn't render anything visible - it registers edge configuration with the graph on mount and unregisters on unmount.

### Port Registration

Port components register themselves in a shared context so that `LinkEdgeConfig` can query available ports on target nodes. This enables the `targetPort` picker function pattern:

```tsx
<LinkEdgeConfig
  linkKey={link.linkKey}
  sourcePort="out"
  targetPort={(availablePorts) => availablePorts.includes("in") ? "in" : null}
/>
```

### Fallback Behavior

When a thing type has no custom `graph` renderer:
- Render a simple colored square with shortId (same as current behavior)
- Use type color from `configure_rendering()`
- Hidden default ports - edges connect to node center
- No edge customization (default styling)

---

## Implementation Steps

### 1. Add TypeScript types for graph renderers

**Files:** `thingwatcher/thingwatcher-bindings.d.ts`

Add new types:

```typescript
// Props for graph node renderer (same handle as ref)
export interface GraphNodeRendererProps {
  handle: RefRendererHandle;
}

// Props for LinkEdgeConfig component
export interface LinkEdgeConfigProps {
  /** The link key (base64) this config applies to */
  linkKey: string;
  /** Source port ID on this node */
  sourcePort: string;
  /** Target port ID, or function to pick from available target ports */
  targetPort: string | ((availablePorts: string[]) => string | null);
  /** Text label for the edge (optional) */
  label?: string;
  /** CSS styles for the edge path (optional) */
  style?: React.CSSProperties;
  /** Marker at the end of the edge (optional, defaults to arrow) */
  markerEnd?: EdgeMarkerType;
}
```

Update `RendererSet`:

```typescript
export interface RendererSet {
  full?: FC<FullRendererProps>;
  ref?: FC<RefRendererProps>;
  inline?: FC<RefRendererProps>;
  preview?: FC<PreviewRendererProps>;
  graph?: FC<GraphNodeRendererProps>;  // NEW - just the node component
}
```

- [x] Add `GraphNodeRendererProps` interface
- [x] Add `LinkEdgeConfigProps` interface
- [x] Update `RendererSet` to include `graph` field (just an FC, not a config object)

### 2. Export Port, PortPosition, and LinkEdgeConfig from thingwatcher-bindings

**Files:**
- `thingwatcher/thingwatcher-bindings.d.ts`
- `thingwatcher/frontend/lib/ThingWatcherBindingsExternal.tsx`

```typescript
// Re-export React Flow's Handle as Port
export { Handle as Port, Position as PortPosition } from '@xyflow/react';
export type { HandleProps as PortProps } from '@xyflow/react';

// LinkEdgeConfig - declarative edge configuration
export declare const LinkEdgeConfig: FC<LinkEdgeConfigProps>;
```

The Port component will be wrapped to add registration logic (see step 4).

- [x] Re-export `Handle` as `Port` from ThingWatcherBindingsExternal.tsx
- [x] Re-export `Position` as `PortPosition`
- [x] Export `LinkEdgeConfig` component
- [x] Add corresponding types to thingwatcher-bindings.d.ts with JSDoc

### 3. Create GraphEdgeConfigContext for registration

**Files:** `thingwatcher/frontend/components/Graph/GraphEdgeConfigContext.tsx`

Create a context for edge config and port registration:

```typescript
interface EdgeConfig {
  linkKey: string;
  sourcePort: string;
  targetPort: string | ((availablePorts: string[]) => string | null);
  label?: string;
  style?: React.CSSProperties;
  markerEnd?: EdgeMarkerType;
}

interface PortInfo {
  nodeId: string;
  portId: string;
  type: 'source' | 'target';
}

interface GraphEdgeConfigContextValue {
  // Edge config registration
  registerEdgeConfig: (nodeId: string, config: EdgeConfig) => void;
  unregisterEdgeConfig: (nodeId: string, linkKey: string) => void;

  // Port registration
  registerPort: (info: PortInfo) => void;
  unregisterPort: (nodeId: string, portId: string) => void;
  getTargetPorts: (nodeId: string) => string[];
}
```

The context stores:
- Map of nodeId -> Map of linkKey -> EdgeConfig
- Map of nodeId -> Set of port IDs (by type)

- [x] Create `GraphEdgeConfigContext.tsx`
- [x] Implement edge config registration
- [x] Implement port registration
- [x] Export context and provider

### 4. Create Port wrapper with registration

**Files:** `thingwatcher/frontend/lib/Port.tsx`

Wrap React Flow's Handle to add port registration:

```typescript
import { Handle, type HandleProps } from '@xyflow/react';
import { useNodeId } from '@xyflow/react';
import { useGraphEdgeConfigContext } from '../components/Graph/GraphEdgeConfigContext';

export function Port(props: HandleProps) {
  const nodeId = useNodeId();
  const { registerPort, unregisterPort } = useGraphEdgeConfigContext();

  useEffect(() => {
    if (nodeId && props.id) {
      registerPort({ nodeId, portId: props.id, type: props.type });
      return () => unregisterPort(nodeId, props.id);
    }
  }, [nodeId, props.id, props.type]);

  return <Handle {...props} />;
}
```

Note: `useNodeId()` is a React Flow hook that returns the current node's ID when called inside a custom node component.

- [x] Create `Port.tsx` wrapper component
- [x] Register port on mount, unregister on unmount
- [x] Use `useNodeId()` to get current node ID
- [x] Handle case where context is not available (outside graph)

### 5. Create LinkEdgeConfig component

**Files:** `thingwatcher/frontend/lib/LinkEdgeConfig.tsx`

Component that registers edge configuration:

```typescript
export function LinkEdgeConfig(props: LinkEdgeConfigProps) {
  const nodeId = useNodeId();
  const { registerEdgeConfig, unregisterEdgeConfig } = useGraphEdgeConfigContext();

  useEffect(() => {
    if (nodeId) {
      registerEdgeConfig(nodeId, {
        linkKey: props.linkKey,
        sourcePort: props.sourcePort,
        targetPort: props.targetPort,
        label: props.label,
        style: props.style,
        markerEnd: props.markerEnd,
      });
      return () => unregisterEdgeConfig(nodeId, props.linkKey);
    }
  }, [nodeId, props.linkKey, props.sourcePort, props.targetPort, props.label, props.style, props.markerEnd]);

  return null;  // Renders nothing
}
```

- [x] Create `LinkEdgeConfig.tsx`
- [x] Register config on mount, unregister on unmount
- [x] Use `useNodeId()` to get source node ID
- [x] Return null (renders nothing visible)

### 6. Create DynamicGraphNodeRenderer component

**Files:** `thingwatcher/frontend/components/Graph/DynamicGraphNodeRenderer.tsx`

Component that fetches and renders custom graph nodes:

```typescript
interface DynamicGraphNodeRendererProps {
  thingKey: string;
  thingType: string;
  shortId: number;
}

function DynamicGraphNodeRenderer({ thingKey, thingType, shortId }: DynamicGraphNodeRendererProps) {
  // 1. Fetch renderer module (like other DynamicRenderers)
  const { module, loading } = useRendererModule(thingType);

  // 2. Get graph renderer from module
  const graphRenderer = module?.renderers[thingType]?.graph ?? null;

  // 3. If no custom renderer, render fallback
  if (!graphRenderer) {
    return <FallbackGraphNode shortId={shortId} thingType={thingType} />;
  }

  // 4. Create RefRendererHandle (not Preview - graph nodes are ref-like)
  // 5. Wrap in ShadowRootDiv for style isolation
  // 6. Render the custom node component with ErrorBoundary
}
```

This follows the same pattern as other DynamicRenderer components - each node fetches its own renderer.

- [x] Create `DynamicGraphNodeRenderer.tsx` in `components/Graph/`
- [x] Fetch renderer using existing module fetch pattern
- [x] Render FallbackGraphNode if no custom graph renderer
- [x] Create RefRendererHandle for the thing
- [x] Wrap custom renderer in ShadowRootDiv with ErrorBoundary

### 7. Create FallbackGraphNode component

**Files:** `thingwatcher/frontend/components/Graph/FallbackGraphNode.tsx`

Fallback when no custom graph renderer exists:

```typescript
import { Handle, Position } from '@xyflow/react';

function FallbackGraphNode({ shortId, thingType }: FallbackGraphNodeProps) {
  const { config } = useThingTypeConfig(thingType);
  const color = config?.color ?? DEFAULT_COLOR;

  return (
    <div className={styles.fallbackNode} style={{ backgroundColor: color }}>
      <Handle type="target" position={Position.Top} className={styles.handle} />
      {shortId}
      <Handle type="source" position={Position.Bottom} className={styles.handle} />
    </div>
  );
}
```

Note: Uses React Flow's Handle directly (not our Port wrapper) since fallback doesn't need port registration - it uses default ports.

- [x] Create `FallbackGraphNode.tsx` in `components/Graph/`
- [x] Include hidden Handle components for edge connections
- [ ] Export from `components/Graph/index.ts` (not needed - internal component)

### 8. Update ThingNode to use DynamicGraphNodeRenderer

**Files:** `thingwatcher/frontend/components/Graph/ThingNode.tsx`

Update ThingNode to render DynamicGraphNodeRenderer:

```typescript
export const ThingNode = memo(function ThingNode({ data }: NodeProps<ThingNodeType>) {
  const { shortId, thingType, thingKey, selected, changeState } = data;

  return (
    <div className={classNames.join(' ')}>
      <DynamicGraphNodeRenderer
        thingKey={thingKey}
        thingType={thingType}
        shortId={shortId}
      />
    </div>
  );
});
```

ThingNode is just a styling wrapper for change-state/selection. The actual content comes from DynamicGraphNodeRenderer.

- [x] Update ThingNode to render DynamicGraphNodeRenderer
- [x] ThingNode handles change-state/selection styling only
- [x] Ensure styling works for both custom and fallback nodes

### 9. Update Graph.tsx to provide context and apply edge configs

**Files:** `thingwatcher/frontend/components/Graph/Graph.tsx`

Wrap ReactFlow in the edge config context provider and apply registered configs to edges:

```typescript
function GraphInner({ things, ... }: GraphProps) {
  // Edge config state
  const [edgeConfigs, setEdgeConfigs] = useState<Map<string, Map<string, EdgeConfig>>>(new Map());
  const [portRegistry, setPortRegistry] = useState<Map<string, Set<string>>>(new Map());

  // Context value with registration functions
  const contextValue = useMemo(() => ({
    registerEdgeConfig: (nodeId, config) => { ... },
    unregisterEdgeConfig: (nodeId, linkKey) => { ... },
    registerPort: (info) => { ... },
    unregisterPort: (nodeId, portId) => { ... },
    getTargetPorts: (nodeId) => [...(portRegistry.get(nodeId) ?? [])],
  }), [portRegistry]);

  // Apply edge configs to edges
  const configuredEdges = useMemo(() => {
    return baseEdges.map(edge => {
      const sourceConfigs = edgeConfigs.get(edge.source);
      const config = sourceConfigs?.get(edge.data?.linkKey);

      if (!config) return edge;  // Use defaults

      // Resolve targetPort if it's a function
      let targetPort = config.targetPort;
      if (typeof targetPort === 'function') {
        const availablePorts = getTargetPorts(edge.target);
        targetPort = targetPort(availablePorts);
      }

      return {
        ...edge,
        sourceHandle: config.sourcePort,
        targetHandle: targetPort,
        label: config.label,
        style: config.style,
        markerEnd: config.markerEnd ?? edge.markerEnd,
      };
    });
  }, [baseEdges, edgeConfigs, portRegistry]);

  return (
    <GraphEdgeConfigContext.Provider value={contextValue}>
      <ReactFlow edges={configuredEdges} ... />
    </GraphEdgeConfigContext.Provider>
  );
}
```

- [x] Add GraphEdgeConfigContext.Provider to Graph.tsx
- [x] Implement edge config and port registration state
- [x] Apply registered configs to edges
- [x] Handle targetPort picker function

### 10. Propagate node dimensions to cola layout

**Files:**
- `thingwatcher/frontend/components/Graph/Graph.tsx`
- `thingwatcher/frontend/components/Graph/useColaLayout.ts`

React Flow measures node dimensions after render. We need to propagate these to cola for proper overlap avoidance:

```typescript
// In Graph.tsx onNodesChange handler
for (const change of changes) {
  if (change.type === 'dimensions' && change.dimensions) {
    updateNodeDimensions(change.id, change.dimensions.width, change.dimensions.height);
  }
}
```

The useColaLayout hook needs a new method to accept dimension updates:

```typescript
// In useColaLayout
updateNodeDimensions: (nodeId: string, width: number, height: number) => void;
```

- [x] Add `updateNodeDimensions` method to useColaLayout
- [x] Handle dimension changes in Graph.tsx onNodesChange
- [x] Cola re-runs layout when dimensions change significantly

### 11. Update container_item demo with graph renderers

**Files:** `examples/demo_dynamic_rendering/Container.tsx`, `examples/demo_dynamic_rendering/Item.tsx`, `examples/demo_dynamic_rendering/container_item.ts`

Add graph renderers using the new declarative pattern.

Note: Graph nodes use RefRendererHandle, so no `useDraggableIdMarker`.

```typescript
import {
  Port, PortPosition, LinkEdgeConfig,
  usePayload, useLinks, useThingMetadata
} from 'thingwatcher-bindings';
import type { GraphNodeRendererProps } from 'thingwatcher-bindings';

function ContainerGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);

  const itemLinks = links.filter(l => l.linkType === 'item');

  return (
    <div className="container-graph-node">
      <Port type="target" position={PortPosition.Top} id="in" />
      <span>#{shortId} {payload?.name}</span>
      <Port type="source" position={PortPosition.Bottom} id="item" />

      {/* Declarative edge config for each item link */}
      {itemLinks.map(link => (
        <LinkEdgeConfig
          key={link.linkKey}
          linkKey={link.linkKey}
          sourcePort="item"
          targetPort="in"
          label="item"
          style={{ stroke: '#4ecdc4' }}
        />
      ))}
    </div>
  );
}

function ItemGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const hue = (payload?.value || 0) * 30;

  const containerLink = links.find(l => l.linkType === 'container');

  return (
    <div style={{ background: `hsl(${hue}, 70%, 30%)`, padding: 8, borderRadius: 4 }}>
      <Port type="target" position={PortPosition.Top} id="in" />
      <span>#{shortId}</span>
      <Port type="source" position={PortPosition.Bottom} id="container" />

      {containerLink && (
        <LinkEdgeConfig
          linkKey={containerLink.linkKey}
          sourcePort="container"
          targetPort="in"
        />
      )}
    </div>
  );
}

// Update module export
export default {
  dialect: "0.1",
  renderers: {
    "Container": {
      full: ContainerFull,
      ref: ContainerRef,
      inline: ContainerInline,
      preview: ContainerPreview,
      graph: ContainerGraphNode,  // Just the component, no config object
    },
    "Item": {
      full: ItemFull,
      ref: ItemRef,
      inline: ItemInline,
      preview: ItemPreview,
      graph: ItemGraphNode,
    },
  }
} as ThingWatcherRenderConfig;
```

- [x] Add ContainerGraphNode with Port and LinkEdgeConfig
- [x] Add ItemGraphNode with Port and LinkEdgeConfig
- [x] Update export with graph renderers (just FC, not config object)

### 12. Update documentation

**Files:**
- `thingwatcher/frontend/LOCAL_CODE_OVERVIEW.md`
- `CODE_OVERVIEW.md`
- `thingwatcher/thingwatcher-bindings.d.ts` (inline docs)

- [x] Update LOCAL_CODE_OVERVIEW.md: Graph section for custom renderers
- [x] Update CODE_OVERVIEW.md: mention graph renderer type
- [x] Ensure thingwatcher-bindings.d.ts has good JSDoc for new types

### 13. Test

- [ ] Run demo_dynamic_rendering
- [ ] Verify Container nodes show custom graph node with ports
- [ ] Verify Item nodes show custom graph node with ports
- [ ] Verify edges connect to correct ports (via LinkEdgeConfig)
- [ ] Verify edge labels and styles apply
- [ ] Verify normal drag moves node in graph
- [ ] Verify fallback works for types without graph renderer
- [ ] Verify change state styling still works on custom graph nodes
- [ ] Verify selection styling still works on custom graph nodes
- [ ] Verify custom node sizes are respected by cola layout (no overlapping)

---

## Technical Notes

### Registration Flow

1. Graph.tsx renders ReactFlow with GraphEdgeConfigContext.Provider
2. ReactFlow renders ThingNode components
3. ThingNode renders DynamicGraphNodeRenderer
4. DynamicGraphNodeRenderer renders custom graph node (or fallback)
5. Custom graph node renders Port and LinkEdgeConfig components
6. Port registers itself with context on mount
7. LinkEdgeConfig registers edge config with context on mount
8. Graph.tsx reacts to config changes and updates edges
9. On unmount, Port and LinkEdgeConfig unregister

### Port Registration Details

The Port wrapper uses `useNodeId()` (React Flow hook) to get the current node ID. This hook only works inside a custom node component rendered by ReactFlow.

Port registration stores: `{ nodeId, portId, type: 'source' | 'target' }`

This allows LinkEdgeConfig's `targetPort` picker function to query available target ports on any node.

### Edge Config Application

When applying edge configs:
1. Look up config by source node ID + link key
2. If no config, use edge defaults (center connection, default styling)
3. If config exists:
   - Set `sourceHandle` from config
   - Resolve `targetPort` (call function if needed, passing available ports)
   - Set `targetHandle` to resolved value
   - Apply label, style, markerEnd

### Context Outside Graph

Port and LinkEdgeConfig need to handle being rendered outside the graph context (e.g., in tests or other views). They should:
- Check if context is available
- No-op if context is missing (don't crash)
- Port still renders the Handle (just without registration)

---

## Implementation Notes / Risks

### Custom Node Sizing and Cola Layout

Cola.js needs node dimensions for overlap avoidance. Strategy:
- React Flow measures nodes after render (`.measured.width`, `.measured.height`)
- Send measured dimensions back to cola, similar to how we send drag position updates
- Cola will re-run layout with correct dimensions
- This means layout may shift slightly after initial render as dimensions are measured

### Change State Wrapper Structure

The intended DOM structure:
```
<div class="thingNode added selected">  <!-- wrapper: light DOM, has borders/shadows -->
  <DynamicGraphNodeRenderer>
    <shadow-root>
      <custom-node-content />  <!-- custom renderer: shadow DOM, isolated styles -->
    </shadow-root>
  </DynamicGraphNodeRenderer>
</div>
```

The change-state borders and selection box-shadow go on the outer wrapper, which is in light DOM and can be styled by app CSS.

### Ports in Shadow DOM (Risk)

React Flow may expect Handle/Port components to be in light DOM for edge routing. If shadow DOM causes issues:
- Fallback: Slot Port components out to light DOM (like we do with NodeRef)
- This would require wrapping Port in ShadowSlot in ThingWatcherBindingsExternal

Try shadow DOM first; if edges don't connect to ports correctly, implement the slot-out approach.

### Edge Config Registration Timing

Edge configs are registered on mount, which happens after the initial render. This means:
- First render: edges use defaults (center connection)
- After mount: edges update to use registered configs

This is fine - the transition should be quick and possibly imperceptible.

---

## Exit Criteria

- [ ] Custom graph renderers work - Container/Item show custom nodes with ports
- [ ] Port registration enables querying available ports on any node
- [ ] LinkEdgeConfig declaratively configures edges (ports, labels, styles)
- [ ] targetPort picker function works with available ports
- [ ] Node dimensions propagated to cola layout (custom-sized nodes don't overlap)
- [ ] Fallback works for types without graph renderer
- [ ] Change state and selection styling work on all nodes
- [ ] Documentation updated (LOCAL_CODE_OVERVIEW.md, CODE_OVERVIEW.md, thingwatcher-bindings.d.ts)

**Deferred to Phase D:**
- Alt+drag for drag-to-panel
- Hover preview cards on graph nodes

---

## Issue: Render Loop in GraphEdgeConfigContext

### How the Version State System Works

The `GraphEdgeConfigContext` uses a "version" state pattern to trigger re-computation of edges when configs change:

1. **Data Storage (refs)**: Edge configs and port registrations are stored in refs (`edgeConfigsRef`, `portRegistryRef`). Refs don't trigger re-renders when mutated.

2. **Version Counter (state)**: A `version` state counter is incremented via `notifyChange()` whenever data changes. This triggers a re-render.

3. **Consumer Reaction**: `Graph.tsx` reads `configVersion = edgeConfigCtx?.getVersion()` and includes it in the dependency check for edge sync. When version changes, edges are recomputed with the new configs.

4. **Context Value**: The context value is memoized and includes all the registration functions plus `getVersion()`.

### The Loop Problem

When a `Port` component mounts, it:
1. Calls `registerPort()` in a `useEffect`
2. `registerPort()` calls `notifyChange()` which does `setVersion(v => v + 1)`
3. This re-renders `GraphEdgeConfigProvider`
4. The context value object is recreated (even though it's memoized, the functions have new identities if their deps change)
5. Port's `useEffect` has `ctx` in its dependency array
6. The effect cleanup runs (calling `unregisterPort`)
7. `unregisterPort` calls `notifyChange()` which does `setVersion(v => v + 1)`
8. **Loop!**

The same loop occurs with `LinkEdgeConfig`.

### Why `ctx` is in the Dependency Array

The Port component does:
```typescript
useEffect(() => {
  if (ctx && nodeId) {
    ctx.registerPort({ nodeId, portId, type: props.type });
    return () => ctx.unregisterPort(nodeId, portId);
  }
}, [ctx, nodeId, props.id, props.type]);
```

Including `ctx` is technically correct per React rules (the effect uses `ctx`), but it creates the loop when `ctx` changes on every render.

### Potential Fixes

1. **Don't notify on port registration**: Port registration is only needed for `getTargetPorts()` queries. If we don't call `notifyChange()` for ports, there's no loop. Edge configs would still trigger re-renders, and by the time edges are applied, ports should be registered.

2. **Stabilize the context value**: Make sure the context value object identity doesn't change unless truly necessary. This requires careful memoization of all functions.

3. **Remove `ctx` from Port's dependency array**: Use a ref to store ctx and access it in the effect. This is technically an ESLint violation but would work.

4. **Debounce/batch notifications**: Use `queueMicrotask` or similar to batch multiple registrations into a single version bump.

5. **Don't use version state at all**: Have `Graph.tsx` subscribe to changes differently, or just re-apply edge configs on every render (might be fine for performance).

---

## Fix: Apply the Registry Pattern

See `docs/registry_pattern.md` for the general pattern. Here's how to apply it:

### New file: `lib/Registry.tsx`

A shared registry module with the generic Registry class and hooks:

```typescript
import { createContext, useContext, useRef, useEffect, useCallback, useSyncExternalStore } from 'react';

type Listener = () => void;

export class Registry {
  private data = new Map<string, unknown[]>();
  private listeners = new Map<string, Set<Listener>>();
  private static readonly EMPTY: readonly never[] = Object.freeze([]);

  register<V>(key: string, value: V): void {
    const list = this.data.get(key) ?? [];
    this.data.set(key, [...list, value]);
    this.notify(key);
  }

  unregister<V>(key: string, value: V): void {
    const list = this.data.get(key);
    if (!list) return;
    const idx = list.indexOf(value);
    if (idx >= 0) {
      const newList = list.filter((_, i) => i !== idx);
      if (newList.length === 0) {
        this.data.delete(key);
      } else {
        this.data.set(key, newList);
      }
      this.notify(key);
    }
  }

  subscribe(key: string, listener: Listener): () => void {
    let listeners = this.listeners.get(key);
    if (!listeners) {
      listeners = new Set();
      this.listeners.set(key, listeners);
    }
    listeners.add(listener);
    return () => {
      listeners!.delete(listener);
      if (listeners!.size === 0) {
        this.listeners.delete(key);
      }
    };
  }

  getSnapshot<V>(key: string): V[] {
    return (this.data.get(key) ?? Registry.EMPTY) as V[];
  }

  private notify(key: string): void {
    this.listeners.get(key)?.forEach(listener => listener());
  }
}

/**
 * Register a value with a registry on mount, unregister on unmount.
 * Value should be memoized to avoid unnecessary re-registrations.
 */
export function useRegister<V>(
  registry: Registry | null,
  key: string,
  value: V | null
): void {
  useEffect(() => {
    if (!registry || value === null) return;
    registry.register(key, value);
    return () => registry.unregister(key, value);
  }, [registry, key, value]);
}

/**
 * Subscribe to all values registered under a key.
 * Returns a stable array that only changes when contents change.
 */
export function useRegistrySnapshot<V>(
  registry: Registry | null,
  key: string
): V[] {
  const subscribe = useCallback(
    (listener: () => void) => registry?.subscribe(key, listener) ?? (() => {}),
    [registry, key]
  );

  const getSnapshot = useCallback(
    () => registry?.getSnapshot<V>(key) ?? [],
    [registry, key]
  );

  return useSyncExternalStore(subscribe, getSnapshot);
}
```

### Simplified GraphEdgeConfigContext.tsx

Now just provides the registry and types:

```typescript
import { createContext, useContext, useRef } from 'react';
import { Registry } from '../../lib/Registry';

// --- Types ---

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

// --- Context ---

const GraphRegistryContext = createContext<Registry | null>(null);

export function GraphEdgeConfigProvider({ children }: { children: React.ReactNode }) {
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

export function useGraphRegistry(): Registry | null {
  return useContext(GraphRegistryContext);
}
```

### Changes to Port.tsx

```typescript
import { useNodeId, Handle, type HandleProps } from '@xyflow/react';
import { useMemo } from 'react';
import { useRegister } from './Registry';
import { useGraphRegistry, PORTS_KEY, type PortInfo } from '../components/Graph/GraphEdgeConfigContext';

export function Port(props: HandleProps) {
  const nodeId = useNodeId();
  const registry = useGraphRegistry();
  const portId = props.id ?? (props.type === 'source' ? '__default_source__' : '__default_target__');

  // Memoize the port info to avoid re-registration on every render
  const portInfo = useMemo<PortInfo | null>(
    () => nodeId ? { nodeId, portId, type: props.type } : null,
    [nodeId, portId, props.type]
  );

  useRegister(registry, PORTS_KEY, portInfo);

  return <Handle {...props} />;
}
```

### Changes to LinkEdgeConfig.tsx

```typescript
import { useNodeId } from '@xyflow/react';
import { useMemo } from 'react';
import { useRegister } from './Registry';
import { useGraphRegistry, EDGE_CONFIGS_KEY, type EdgeConfig } from '../components/Graph/GraphEdgeConfigContext';

export function LinkEdgeConfig(props: LinkEdgeConfigProps) {
  const nodeId = useNodeId();
  const registry = useGraphRegistry();

  // Memoize the edge config to avoid re-registration on every render
  // Note: If props.style is a new object each render, this will still re-register.
  // Callers should memoize style objects.
  const edgeConfig = useMemo<EdgeConfig | null>(
    () => nodeId ? {
      linkKey: props.linkKey,
      sourcePort: props.sourcePort,
      targetPort: props.targetPort,
      label: props.label,
      style: props.style,
    } : null,
    [nodeId, props.linkKey, props.sourcePort, props.targetPort, props.label, props.style]
  );

  useRegister(registry, EDGE_CONFIGS_KEY, edgeConfig);

  return null;
}
```

### Changes to Graph.tsx

Replace the version-based edge sync with the generic hooks:

```typescript
import { useRegistrySnapshot } from '../../lib/Registry';
import {
  GraphEdgeConfigProvider,
  useGraphRegistry,
  EDGE_CONFIGS_KEY,
  PORTS_KEY,
  type EdgeConfig,
  type PortInfo,
} from './GraphEdgeConfigContext';

function GraphInner({ ... }) {
  const registry = useGraphRegistry();
  const edgeConfigs = useRegistrySnapshot<EdgeConfig>(registry, EDGE_CONFIGS_KEY);
  const ports = useRegistrySnapshot<PortInfo>(registry, PORTS_KEY);

  // Helper to get target ports for a node
  const getTargetPorts = useCallback(
    (nodeId: string) => ports
      .filter(p => p.nodeId === nodeId && p.type === 'target')
      .map(p => p.portId),
    [ports]
  );

  // Apply edge configs
  const configuredEdges = useMemo(() => {
    // Build lookup: linkKey -> config
    const configByLinkKey = new Map<string, EdgeConfig>();
    for (const config of edgeConfigs) {
      configByLinkKey.set(config.linkKey, config);
    }

    return baseEdges.map(edge => {
      const config = configByLinkKey.get(edge.data?.linkKey);
      if (!config) return edge;

      let targetPort = config.targetPort;
      if (typeof targetPort === 'function') {
        targetPort = targetPort(getTargetPorts(edge.target));
      }

      return {
        ...edge,
        sourceHandle: config.sourcePort,
        targetHandle: targetPort,
        label: config.label,
        style: config.style,
      };
    });
  }, [baseEdges, edgeConfigs, getTargetPorts]);

  // ...
}
```

### Why This Fixes the Loop

1. **Registry is stable**: Created once in a ref, context value never changes
2. **Port/LinkEdgeConfig effects don't re-run**: Their deps (registry, memoized value) are stable
3. **Only Graph.tsx re-renders**: When configs change, `useSyncExternalStore` triggers re-render only in Graph.tsx
4. **No cascade**: Graph.tsx re-rendering doesn't affect Port/LinkEdgeConfig (they're children of ReactFlow nodes, not Graph.tsx directly)
