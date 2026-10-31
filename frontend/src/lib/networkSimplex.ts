/**
 * Network simplex, as Gansner et al. use it for layered drawing ("A Technique for Drawing Directed
 * Graphs", 1993): values for the nodes of a connected DAG that satisfy
 * `value[w] - value[v] >= minlen` for every edge `v → w` and minimize the sum of
 * `weight * (value[w] - value[v])`. With integer `minlen`s every value is an integer.
 *
 * It keeps a spanning tree of tight edges (edges at exactly their `minlen`). Each tree edge's cut
 * value is what the objective would gain per unit of stretching that edge; while some cut value is
 * negative, that edge leaves the tree and the slackest-to-tighten edge across the same cut enters.
 */
export interface SimplexEdge {
  v: number
  w: number
  minlen: number
  weight: number
}

export function networkSimplex(nodeCount: number, edges: readonly SimplexEdge[]): number[] {
  const value = longestPath(nodeCount, edges)
  const incident: number[][] = Array.from({ length: nodeCount }, () => [])
  for (const [index, edge] of edges.entries()) {
    incident[edge.v].push(index)
    incident[edge.w].push(index)
  }
  const slack = (index: number) => value[edges[index].w] - value[edges[index].v] - edges[index].minlen
  const other = (index: number, node: number) => (edges[index].v === node ? edges[index].w : edges[index].v)

  // A feasible tree: grow a tree of tight edges from node 0; when it can grow no further, shift the
  // whole tree to tighten the least slack edge leaving it.
  const inTree = new Array<boolean>(nodeCount).fill(false)
  const isTree = new Array<boolean>(edges.length).fill(false)
  const treeNodes = [0]
  inTree[0] = true
  const grow = () => {
    const pending = [...treeNodes]
    while (pending.length > 0) {
      const node = pending.pop()!
      for (const index of incident[node]) {
        const next = other(index, node)
        if (inTree[next] || slack(index) !== 0) continue
        inTree[next] = true
        isTree[index] = true
        treeNodes.push(next)
        pending.push(next)
      }
    }
  }
  grow()
  while (treeNodes.length < nodeCount) {
    let tightest = -1
    for (let index = 0; index < edges.length; index += 1) {
      if (inTree[edges[index].v] === inTree[edges[index].w]) continue
      if (tightest < 0 || slack(index) < slack(tightest)) tightest = index
    }
    if (tightest < 0) throw new Error('network simplex needs a connected graph')
    const delta = inTree[edges[tightest].v] ? slack(tightest) : -slack(tightest)
    for (const node of treeNodes) value[node] += delta
    grow()
  }

  // The tree rooted at node 0: `lim` numbers nodes in postorder and `low` is the smallest `lim` in
  // a node's subtree, so a node is in the subtree of another exactly when its `lim` is in that
  // one's [low, lim].
  const parentEdge = new Array<number>(nodeCount).fill(-1)
  const low = new Array<number>(nodeCount).fill(0)
  const lim = new Array<number>(nodeCount).fill(0)
  let preorder: number[] = []
  const cutValue = new Array<number>(edges.length).fill(0)
  const indexTree = () => {
    preorder = []
    const postorder: number[] = []
    const stack: Array<{ node: number; next: number }> = [{ node: 0, next: 0 }]
    parentEdge[0] = -1
    preorder.push(0)
    while (stack.length > 0) {
      const top = stack.at(-1)!
      const edgeIndex = incident[top.node][top.next++]
      if (edgeIndex === undefined) {
        stack.pop()
        postorder.push(top.node)
        continue
      }
      if (!isTree[edgeIndex] || edgeIndex === parentEdge[top.node]) continue
      const child = other(edgeIndex, top.node)
      parentEdge[child] = edgeIndex
      preorder.push(child)
      stack.push({ node: child, next: 0 })
    }
    for (const [order, node] of postorder.entries()) {
      lim[node] = order + 1
      low[node] = order + 1
    }
    for (const node of postorder) {
      if (parentEdge[node] >= 0) {
        const parent = other(parentEdge[node], node)
        low[parent] = Math.min(low[parent], low[node])
      }
    }
    // A tree edge's cut value, from its child's side: the edge's own weight, plus or minus every
    // other edge that crosses the same cut, which the child's own tree edges already sum up.
    for (const node of postorder) {
      const own = parentEdge[node]
      if (own < 0) continue
      const childIsTail = edges[own].v === node
      let cut = edges[own].weight
      for (const edgeIndex of incident[node]) {
        if (edgeIndex === own) continue
        const pointsToHead = (edges[edgeIndex].v === node) === childIsTail
        cut += pointsToHead ? edges[edgeIndex].weight : -edges[edgeIndex].weight
        if (isTree[edgeIndex]) cut += pointsToHead ? -cutValue[edgeIndex] : cutValue[edgeIndex]
      }
      cutValue[own] = cut
    }
  }
  const inSubtree = (node: number, root: number) => low[root] <= lim[node] && lim[node] <= lim[root]

  indexTree()
  // Degenerate pivots can in principle cycle; the bound keeps a feasible (if not optimal) answer.
  for (let pivot = 0; pivot < 20 * edges.length; pivot += 1) {
    const leaving = edges.findIndex((_, edgeIndex) => isTree[edgeIndex] && cutValue[edgeIndex] < 0)
    if (leaving < 0) break
    const { v, w } = edges[leaving]
    const flip = lim[v] > lim[w]
    const subtree = flip ? w : v
    let entering = -1
    for (let edgeIndex = 0; edgeIndex < edges.length; edgeIndex += 1) {
      const edge = edges[edgeIndex]
      if (flip !== inSubtree(edge.v, subtree) || flip === inSubtree(edge.w, subtree)) continue
      if (entering < 0 || slack(edgeIndex) < slack(entering)) entering = edgeIndex
    }
    isTree[leaving] = false
    isTree[entering] = true
    indexTree()
    for (const node of preorder) {
      const edgeIndex = parentEdge[node]
      if (edgeIndex < 0) continue
      const edge = edges[edgeIndex]
      value[node] = edge.v === node ? value[edge.w] - edge.minlen : value[edge.v] + edge.minlen
    }
  }
  return value
}

/** The smallest values satisfying every edge, from the sources down. */
function longestPath(nodeCount: number, edges: readonly SimplexEdge[]): number[] {
  const outgoing: number[][] = Array.from({ length: nodeCount }, () => [])
  const remaining = new Array<number>(nodeCount).fill(0)
  for (const [index, edge] of edges.entries()) {
    outgoing[edge.v].push(index)
    remaining[edge.w] += 1
  }
  const value = remaining.map((count) => (count === 0 ? 0 : Number.NEGATIVE_INFINITY))
  const ready = remaining.flatMap((count, node) => (count === 0 ? [node] : []))
  let placed = 0
  while (ready.length > 0) {
    const node = ready.pop()!
    placed += 1
    for (const index of outgoing[node]) {
      const { w, minlen } = edges[index]
      value[w] = Math.max(value[w], value[node] + minlen)
      remaining[w] -= 1
      if (remaining[w] === 0) ready.push(w)
    }
  }
  if (placed !== nodeCount) throw new Error('network simplex needs an acyclic graph')
  return value
}
