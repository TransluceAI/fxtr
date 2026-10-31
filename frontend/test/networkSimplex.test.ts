import { describe, expect, it } from 'vitest'
import { networkSimplex, type SimplexEdge } from '../src/lib/networkSimplex'

function cost(values: number[], edges: SimplexEdge[]): number {
  return edges.reduce((total, edge) => total + edge.weight * (values[edge.w] - values[edge.v]), 0)
}

function feasible(values: number[], edges: SimplexEdge[]): boolean {
  return edges.every((edge) => values[edge.w] - values[edge.v] >= edge.minlen)
}

/** A connected DAG on `size` nodes: a random spanning tree with edges either way, plus extras. */
function randomInstance(seed: number, size: number): SimplexEdge[] {
  let state = seed
  const random = () => {
    state = (state * 1664525 + 1013904223) >>> 0
    return state / 0x100000000
  }
  const edge = (v: number, w: number): SimplexEdge => ({
    v,
    w,
    minlen: Math.floor(random() * 7) - 3,
    weight: Math.floor(random() * 4),
  })
  const edges: SimplexEdge[] = []
  for (let node = 1; node < size; node += 1) {
    const other = Math.floor(random() * node)
    edges.push(random() < 0.5 ? edge(other, node) : edge(node, other))
  }
  // Extra edges run along a topological order of the tree, which keeps the graph acyclic.
  const order = topological(size, edges)
  for (let extra = 0; extra < size; extra += 1) {
    const [a, b] = [Math.floor(random() * size), Math.floor(random() * size)]
    if (a === b) continue
    edges.push(order.indexOf(a) < order.indexOf(b) ? edge(a, b) : edge(b, a))
  }
  return edges
}

function topological(size: number, edges: SimplexEdge[]): number[] {
  const remaining = new Array<number>(size).fill(0)
  for (const edge of edges) remaining[edge.w] += 1
  const ready = remaining.flatMap((count, node) => (count === 0 ? [node] : []))
  const order: number[] = []
  while (ready.length > 0) {
    const node = ready.pop()!
    order.push(node)
    for (const edge of edges) if (edge.v === node && --remaining[edge.w] === 0) ready.push(edge.w)
  }
  return order
}

/** The cheapest feasible values with node 0 at 0 and every other node within ±range of it. */
function bruteForce(size: number, edges: SimplexEdge[], range: number): number {
  let best = Number.POSITIVE_INFINITY
  const values = new Array<number>(size).fill(0)
  const visit = (node: number) => {
    if (node === size) {
      if (feasible(values, edges)) best = Math.min(best, cost(values, edges))
      return
    }
    for (let value = -range; value <= range; value += 1) {
      values[node] = value
      visit(node + 1)
    }
  }
  visit(1)
  return best
}

describe('network simplex', () => {
  it('pulls a weighted edge tight and leaves weightless ones slack', () => {
    const edges: SimplexEdge[] = [
      { v: 0, w: 1, minlen: 2, weight: 0 },
      { v: 1, w: 2, minlen: 3, weight: 5 },
      { v: 0, w: 2, minlen: 1, weight: 1 },
    ]
    const values = networkSimplex(3, edges)
    expect(values[2] - values[1]).toBe(3)
    expect(values[2] - values[0]).toBe(5)
  })

  it('handles negative minimum lengths', () => {
    // Node 1 is pulled no further than 10 right of node 0, and node 2 as far right as
    // min(0 + 4, 10 − 2) = 4 allows: (0 − 4) + (10 − 4) = 2.
    const edges: SimplexEdge[] = [
      { v: 0, w: 1, minlen: 10, weight: 0 },
      { v: 2, w: 0, minlen: -4, weight: 1 },
      { v: 2, w: 1, minlen: 2, weight: 1 },
    ]
    const values = networkSimplex(3, edges)
    expect(feasible(values, edges)).toBe(true)
    expect(values[1] - values[0]).toBe(10)
    expect(values[2] - values[0]).toBe(4)
    expect(cost(values, edges)).toBe(2)
  })

  it('finds the optimum of small random instances', () => {
    for (let seed = 1; seed <= 40; seed += 1) {
      const size = 3 + (seed % 3)
      const edges = randomInstance(seed, size)
      const values = networkSimplex(size, edges)
      expect(feasible(values, edges), `seed ${seed}`).toBe(true)
      expect(values.every(Number.isInteger)).toBe(true)
      expect(cost(values, edges), `seed ${seed}`).toBe(bruteForce(size, edges, 3 * size))
    }
  })

  it('refuses a disconnected graph', () => {
    expect(() => networkSimplex(3, [{ v: 0, w: 1, minlen: 1, weight: 1 }])).toThrow(/connected/)
  })
})
