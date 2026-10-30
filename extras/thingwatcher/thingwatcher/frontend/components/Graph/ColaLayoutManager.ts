import * as cola from 'webcola';

// Node dimensions for layout calculation
const NODE_WIDTH = 40;
const NODE_HEIGHT = 40;

// Cola node with position and fixed state
// Note: cola.Node uses `fixed` as a number (0 = not fixed, non-zero = fixed)
interface ColaNode extends cola.Node {
  id: string;
  width: number;
  height: number;
}

// Info needed to add a node
export interface NodeInfo {
  id: string;
  shortId: number;
}

// Info needed for an edge
export interface EdgeInfo {
  source: string;
  target: string;
}

// Simple seeded random number generator (mulberry32)
function seededRandom(seed: number): () => number {
  return function () {
    let t = (seed += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Manages cola.js force-directed layout simulation.
 *
 * This is a plain TypeScript class (not a React hook) that owns all cola state.
 * It persists across React renders and manages the simulation lifecycle.
 */
// Cola link type - source/target can be node index or node object
interface ColaLink {
  source: number | ColaNode;
  target: number | ColaNode;
}

export class ColaLayoutManager {
  private layout: cola.Layout | null = null;
  private colaNodes: ColaNode[] = [];
  private colaLinks: ColaLink[] = [];
  private nodeIndexMap: Map<string, number> = new Map();
  private positions: Map<string, { x: number; y: number }> = new Map();
  private nodeDimensions: Map<string, { width: number; height: number }> = new Map();
  private currentNodeIds: Set<string> = new Set();
  private currentEdges: EdgeInfo[] = [];

  private size = { width: 800, height: 600 };
  private isRunning = false;
  private isDragging = false;
  private animationFrameId: number | null = null;

  private onPositionsChanged: () => void;

  constructor(onPositionsChanged: () => void) {
    this.onPositionsChanged = onPositionsChanged;
  }

  /**
   * Update the layout size (call when container resizes).
   */
  setSize(width: number, height: number): void {
    if (width === 0 || height === 0) return;
    if (this.size.width === width && this.size.height === height) return;

    this.size = { width, height };
    if (this.layout) {
      this.layout.size([width, height]);
      this.ensureRunning();
    }
  }

  /**
   * Sync the node and edge lists with the current state.
   * Diffs against previous state and updates cola accordingly.
   */
  syncGraph(nodes: NodeInfo[], edges: EdgeInfo[]): void {
    const newNodeIds = new Set(nodes.map((n) => n.id));

    // Find added and removed nodes
    const addedNodes = nodes.filter((n) => !this.currentNodeIds.has(n.id));
    const removedIds = [...this.currentNodeIds].filter((id) => !newNodeIds.has(id));

    // Check if edges changed
    const edgesChanged = !this.edgesEqual(edges, this.currentEdges);

    const structureChanged = addedNodes.length > 0 || removedIds.length > 0 || edgesChanged;

    if (!structureChanged) {
      return;
    }

    // console.log('[cola-manager] syncGraph', {
    //   added: addedNodes.map((n) => n.id.slice(0, 10)),
    //   removed: removedIds.map((id) => id.slice(0, 10)),
    //   edgesChanged,
    // });

    // Clean up positions for removed nodes
    for (const id of removedIds) {
      this.positions.delete(id);
    }

    // Calculate initial positions for new nodes
    const edgeMap = this.buildEdgeMap(edges);
    for (const node of addedNodes) {
      const pos = this.calculateInitialPosition(node, edgeMap);
      this.positions.set(node.id, pos);
    }

    // Update tracking
    this.currentNodeIds = newNodeIds;
    this.currentEdges = edges;

    // Rebuild cola layout
    this.rebuildLayout(nodes, edges);
  }

  /**
   * Pin a node at a specific position (for dragging).
   */
  pinNode(id: string, x: number, y: number): void {
    // console.log('[cola-manager] pinNode', { id: id, x: Math.round(x), y: Math.round(y) });
    this.isDragging = true;

    const index = this.nodeIndexMap.get(id);
    if (index === undefined) {
      console.warn('[cola-manager] pinNode: node not found', { id: id });
      return;
    }

    const colaNode = this.colaNodes[index];
    if (!colaNode) {
      console.warn('[cola-manager] pinNode: colaNode not found', { id: id });
      return;
    }

    // Cola uses fixed as a number: 0 = not fixed, non-zero = fixed
    colaNode.fixed = 1;
    colaNode.x = x;
    colaNode.y = y;
    this.positions.set(id, { x, y });
    // console.log('[cola-manager] pinNode', { colaNode, colaNode_fixed: colaNode.fixed, colaNode_x: colaNode.x, colaNode_y: colaNode.y });

    this.ensureRunning();
  }

  /**
   * Unpin a node (after drag ends).
   */
  unpinNode(id: string): void {
    // console.log('[cola-manager] unpinNode', { id: id.slice(0, 10) });
    this.isDragging = false;

    const index = this.nodeIndexMap.get(id);
    if (index === undefined) {
      console.warn('[cola-manager] unpinNode: node not found', { id: id });
      return;
    }

    const colaNode = this.colaNodes[index];
    if (!colaNode) {
      console.warn('[cola-manager] unpinNode: colaNode not found', { id: id });
      return;
    }

    // Cola uses fixed as a number: 0 = not fixed
    colaNode.fixed = 0;

    this.ensureRunning();
    // Update will trigger on tick
    // this.onPositionsChanged();
  }

  /**
   * Update position during drag.
   */
  updateDragPosition(id: string, x: number, y: number): void {
    this.positions.set(id, { x, y });

    const index = this.nodeIndexMap.get(id);
    if (index !== undefined) {
      const colaNode = this.colaNodes[index];
      if (colaNode) {
        colaNode.x = x;
        colaNode.y = y;
        // webcola uses px and py as target positions
        (colaNode as any).px = x;
        (colaNode as any).py = y;
        // console.log('[cola-manager] updateDragPosition: updated at time:', performance.now(), { id: id, x: x, y: y, colaNode, colaNode_fixed: colaNode.fixed, colaNode_x: colaNode.x, colaNode_y: colaNode.y });
      } else {
        console.warn('[cola-manager] updateDragPosition: colaNode not found', { id: id });
      }
    } else {
      console.warn('[cola-manager] updateDragPosition: node not found', { id: id });
    }

    // Trigger immediate re-render for smooth dragging
    // this.onPositionsChanged();

    // Keep simulation running during drag
    this.ensureRunning();
  }

  /**
   * Update node dimensions (for custom-sized nodes).
   * If dimensions change significantly, re-runs layout.
   */
  updateNodeDimensions(id: string, width: number, height: number): void {
    const existing = this.nodeDimensions.get(id);

    // Only update if dimensions changed (with small tolerance)
    if (existing && Math.abs(existing.width - width) < 1 && Math.abs(existing.height - height) < 1) {
      return;
    }

    this.nodeDimensions.set(id, { width, height });

    // Update cola node dimensions
    const index = this.nodeIndexMap.get(id);
    if (index !== undefined) {
      const colaNode = this.colaNodes[index];
      if (colaNode) {
        colaNode.width = width;
        colaNode.height = height;
        // Re-run layout to account for new size
        this.ensureRunning();
      }
    }
  }

  /**
   * Get all current positions.
   */
  getPositions(): Map<string, { x: number; y: number }> {
    return this.positions;
  }

  /**
   * Clean up resources.
   */
  dispose(): void {
    this.stopLoop();
  }

  // --- Private methods ---

  private buildEdgeMap(edges: EdgeInfo[]): { outgoing: Map<string, string[]>; incoming: Map<string, string[]> } {
    const outgoing = new Map<string, string[]>();
    const incoming = new Map<string, string[]>();

    for (const edge of edges) {
      if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
      outgoing.get(edge.source)!.push(edge.target);
      if (!incoming.has(edge.target)) incoming.set(edge.target, []);
      incoming.get(edge.target)!.push(edge.source);
    }

    return { outgoing, incoming };
  }

  private calculateInitialPosition(
    node: NodeInfo,
    edgeMap: { outgoing: Map<string, string[]>; incoming: Map<string, string[]> }
  ): { x: number; y: number } {
    const { width, height } = this.size;
    const centerX = width / 2;
    const centerY = height / 2;

    // Find connected nodes that already have positions
    const connectedIds = [
      ...(edgeMap.outgoing.get(node.id) ?? []),
      ...(edgeMap.incoming.get(node.id) ?? []),
    ].filter((id) => this.positions.has(id));

    if (connectedIds.length > 0) {
      // Average position of connected existing nodes
      let sumX = 0,
        sumY = 0;
      for (const connId of connectedIds) {
        const pos = this.positions.get(connId)!;
        sumX += pos.x;
        sumY += pos.y;
      }
      return {
        x: sumX / connectedIds.length,
        y: sumY / connectedIds.length,
      };
    } else {
      // Orphan node: use seeded random based on shortId
      const rng = seededRandom(node.shortId);
      return {
        x: centerX + (rng() - 0.5) * width * 0.8,
        y: centerY + (rng() - 0.5) * height * 0.8,
      };
    }
  }

  private edgesEqual(a: EdgeInfo[], b: EdgeInfo[]): boolean {
    if (a.length !== b.length) return false;
    // Simple comparison - assumes edges are in same order
    for (let i = 0; i < a.length; i++) {
      if (a[i].source !== b[i].source || a[i].target !== b[i].target) {
        return false;
      }
    }
    return true;
  }

  /**
   * Find connected components and return cola groups for each.
   * Cola will use these groups to keep components separated with padding.
   */
  private buildComponentGroups(edges: EdgeInfo[]): Array<{ leaves: number[]; padding: number }> {
    if (this.colaNodes.length === 0) return [];

    // Build adjacency list
    const adj = new Map<string, Set<string>>();
    for (const node of this.colaNodes) {
      adj.set(node.id, new Set());
    }
    for (const edge of edges) {
      adj.get(edge.source)?.add(edge.target);
      adj.get(edge.target)?.add(edge.source);
    }

    // Find connected components using BFS
    const visited = new Set<string>();
    const components: string[][] = [];

    for (const node of this.colaNodes) {
      if (visited.has(node.id)) continue;

      const component: string[] = [];
      const queue = [node.id];
      visited.add(node.id);

      while (queue.length > 0) {
        const current = queue.shift()!;
        component.push(current);

        for (const neighbor of adj.get(current) ?? []) {
          if (!visited.has(neighbor)) {
            visited.add(neighbor);
            queue.push(neighbor);
          }
        }
      }

      components.push(component);
    }

    // If only one component, no need for groups
    if (components.length <= 1) return [];

    // console.log('[cola-manager] creating groups for', components.length, 'disconnected components');

    // Convert components to cola groups (using node indices)
    return components.map((component) => ({
      leaves: component.map((nodeId) => this.nodeIndexMap.get(nodeId)!).filter((idx) => idx !== undefined),
      padding: 50, // Padding between groups
    }));
  }

  private rebuildLayout(nodes: NodeInfo[], edges: EdgeInfo[], first: boolean = false): void {
    // Stop existing simulation
    this.stopLoop();

    const { width, height } = this.size;
    const centerX = width / 2;
    const centerY = height / 2;

    // Build cola nodes array using existing positions and dimensions
    this.colaNodes = nodes.map((node) => {
      const pos = this.positions.get(node.id) ?? { x: centerX, y: centerY };
      const dims = this.nodeDimensions.get(node.id) ?? { width: NODE_WIDTH, height: NODE_HEIGHT };
      return {
        id: node.id,
        x: pos.x,
        y: pos.y,
        width: dims.width,
        height: dims.height,
        fixed: 0, // Cola uses number: 0 = not fixed
      };
    });

    // Build node index map
    this.nodeIndexMap.clear();
    this.colaNodes.forEach((node, index) => {
      this.nodeIndexMap.set(node.id, index);
    });

    // Build cola links array (using indices)
    this.colaLinks = [];
    for (const edge of edges) {
      const sourceIndex = this.nodeIndexMap.get(edge.source);
      const targetIndex = this.nodeIndexMap.get(edge.target);
      if (sourceIndex !== undefined && targetIndex !== undefined) {
        this.colaLinks.push({
          source: sourceIndex,
          target: targetIndex,
        });
      }
    }

    // Build groups for disconnected components
    const groups = this.buildComponentGroups(edges);

    // Create new layout
    // Cast to any to work around webcola's strict types
    this.layout = new cola.Layout()
      .size([width, height])
      .nodes(this.colaNodes as any)
      .links(this.colaLinks as any)
      .groups(groups as any)
      .linkDistance(120)
      .avoidOverlaps(true)
      .convergenceThreshold(1e-4)
      .handleDisconnected(true);

    // Initialize and start simulation
    // console.log('[cola-manager] rebuildLayout', {
    //   nodeCount: this.colaNodes.length,
    //   linkCount: this.colaLinks.length,
    // });

    // Run a few initial iterations, keep alpha high for async animation
    if (first) {
      this.layout.start(5, 5, 5, 0, true, false); // keepRunning=true preserves alpha
    } else {
      this.layout.start(0, 0, 0, 0, true, false);
    }
    this.startLoop();
  }

  private ensureRunning(): void {
    if (!this.isRunning && this.layout) {
      // console.log('[cola-manager] ensureRunning at time:', performance.now());
      // (this.layout as any).alpha(1.0);
      // @ts-ignoreneed to set it directly to avoid `kick`
      this.layout._alpha = 1.0;
      // console.log('[cola-manager] alpha set to 1.0 at time:', performance.now());
      // this.layout.start(0, 0, 0, 0, true, false);
      // this.layout.resume();
      this.startLoop();
    }
  }

  private startLoop(): void {
    // console.log('[cola-manager] startLoop at time:', performance.now());
    if (this.isRunning) return;
    this.isRunning = true;
    if (this.animationFrameId === null) {
      // console.log('[cola-manager] requestAnimationFrame at time:', performance.now());
      this.animationFrameId = requestAnimationFrame(this.tick);
    }
  }

  private stopLoop(): void {
    this.isRunning = false;
    if (this.animationFrameId !== null) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }
  }

  private tick = (): void => {
    // console.log('[cola-manager] tick called at time:', performance.now());
    if (!this.layout || !this.isRunning) return;

    // console.log('[cola-manager] tick starting at time:', performance.now());

    // Run one iteration
    // Cast to any because webcola types mark tick() as protected but it's callable
    const converged = (this.layout as any).tick() as boolean;

    // console.log('[cola-manager] tick finished at time:', performance.now(), 'converged:', converged, 'isDragging:', this.isDragging);

    // Sync positions from cola nodes to our position map
    for (const colaNode of this.colaNodes) {
      if (colaNode.x !== undefined && colaNode.y !== undefined) {
        this.positions.set(colaNode.id, { x: colaNode.x, y: colaNode.y });
      }
    }

    // Notify React (unless dragging - drag handler triggers updates directly)
    this.onPositionsChanged();

    if (!converged && this.isRunning) {
      this.animationFrameId = requestAnimationFrame(this.tick);
    } else {
      // console.log('[cola-manager] simulation converged');
      this.isRunning = false;
      this.animationFrameId = null;
    }
  };
}
