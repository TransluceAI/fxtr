import { describe, expect, it } from 'vitest'
import {
  CURVE_CLEARANCE,
  DIM_PITCH,
  EDGE_GAP,
  layoutLevel,
  NODE_H,
  SIDE_INPUT_GAP,
  type FlowchartLayout,
  type LayoutEdge,
  type LayoutNode,
  type LayoutPoint,
} from '../src/components/flowchartLayout'
import type { StepDoc } from '../src/types'
import {
  behaviorTransferGraph,
  binaryTree,
  card,
  ccSpoofGraph,
  chain,
  diamonds,
  disconnected,
  fan,
  layered,
  parallelPipelines,
  randomDag,
  skipHeavy,
  workflowFamily,
} from './flowchartGraphs'

const shapes: Record<string, StepDoc[]> = {
  'cc spoof workflow': ccSpoofGraph(),
  'behavior transfer workflow': behaviorTransferGraph(),
  'long chain': chain(24),
  'wide fan-out and fan-in': fan(14),
  'binary tree': binaryTree(5),
  'repeated diamonds': diamonds(8),
  'parallel pipelines': parallelPipelines(5, 6),
  'disconnected components': disconnected(6, 7),
  'dense layered graph': layered(7, 8),
  'many skip-level edges': skipHeavy(20),
  'seeded irregular DAG': randomDag(70, 0x5eed),
}
const family = workflowFamily()
const randomFamily = Array.from({ length: 12 }, (_, index) => randomDag(20 + index * 3, index + 100, 0.06))

describe('flowchart layout invariants', () => {
  const corpus: Array<[string, StepDoc[]]> = [
    ...Object.entries(shapes),
    ...family.map((graph, index): [string, StepDoc[]] => [`workflow-like #${index}`, graph]),
    ...randomFamily.map((graph, index): [string, StepDoc[]] => [`random #${index}`, graph]),
  ]
  for (const [name, graph] of corpus) {
    it(`lays out ${name} cleanly`, () => {
      const layout = layoutLevel(graph)
      expect(layout.nodes).toHaveLength(graph.length)
      expect(layout.edges).toHaveLength(edgeCount(graph))
      expect(Number.isFinite(layout.width) && Number.isFinite(layout.height)).toBe(true)
      expect(violations(layout)).toEqual([])
      expect(layoutLevel(graph)).toEqual(layout)
    })
  }

  it('lays out an empty level', () => {
    expect(layoutLevel([])).toMatchObject({ nodes: [], edges: [] })
  })

  it('draws one edge however many arguments connect two cards', () => {
    expect(layoutLevel([card('source'), card('target', ['source', 'source', 'source'])]).edges).toHaveLength(1)
  })

  it('refuses a dependency cycle', () => {
    expect(() => layoutLevel([card('root'), card('a', ['root', 'b']), card('b', ['a'])])).toThrow(/cycle/)
  })
})

describe('flowchart card heights', () => {
  it('stacks each card\'s dims, so a rank is as tall as its tallest card', () => {
    const layout = layoutLevel([
      card('root'),
      card('short', ['root'], { dims: ['a'] }),
      card('tall', ['root'], { dims: ['a', 'b', 'c'] }),
      card('sink', ['short', 'tall']),
    ])
    const short = layout.byId.get('short')!
    const tall = layout.byId.get('tall')!
    expect(short.height).toBe(NODE_H)
    expect(tall.height).toBe(NODE_H + 2 * DIM_PITCH)
    expect(short.y).toBe(tall.y)
    const edge = layout.edges.find((candidate) => candidate.sourceId === 'short')!
    expect(edge.points[0]!.y).toBe(short.y + short.height)
    expect(edge.points[1]).toEqual({ x: edge.points[0]!.x, y: tall.y + tall.height })
  })
})

describe('flowchart layout ranks', () => {
  it('puts a card with more consumers than inputs just above its first consumer', () => {
    const layout = layoutLevel([
      card('start'),
      card('middle', ['start']),
      card('end', ['middle', 'config']),
      card('config'),
      card('other', ['config']),
    ])
    expect(layout.byId.get('config')!.rank).toBe(layout.byId.get('end')!.rank - 1)
  })

  it('keeps an input with one consumer beside it, to its left', () => {
    const layout = layoutLevel([card('config'), card('shared'), card('run', ['config', 'shared']), card('other', ['shared'])])
    const config = layout.byId.get('config')!
    const run = layout.byId.get('run')!
    expect(config.y).toBe(run.y)
    expect(run.x - config.x - config.width).toBe(SIDE_INPUT_GAP)
    const edge = layout.edges.find((candidate) => candidate.sourceId === 'config')!
    expect(edge.points).toEqual([{ x: config.x + config.width, y: config.y + NODE_H / 2 }, { x: run.x, y: run.y + NODE_H / 2 }])
  })

  it('attaches edges to the consumer of a side input, not the pair', () => {
    const layout = layoutLevel([card('root'), card('trunk', ['root']), card('local_input'), card('consumer', ['trunk', 'local_input'])])
    const trunk = layout.byId.get('trunk')!
    const consumer = layout.byId.get('consumer')!
    expect(center(consumer)).toBe(center(trunk))
  })
})

describe('flowchart layout crossings', () => {
  const planar = ['long chain', 'wide fan-out and fan-in', 'binary tree', 'repeated diamonds', 'parallel pipelines', 'disconnected components']
  for (const name of planar) {
    it(`draws ${name} without crossings`, () => {
      expect(describeCrossings(layoutLevel(shapes[name]))).toEqual([])
    })
  }

  // Measured minima, kept as budgets so a change cannot quietly make these worse. For the two
  // production graphs, 200 random restarts of the ordering find nothing better.
  const budgets: Record<string, number> = {
    'cc spoof workflow': 8,
    'behavior transfer workflow': 2,
  }
  for (const [name, budget] of Object.entries(budgets)) {
    it(`draws ${name} with at most ${budget} crossings`, () => {
      const crossings = describeCrossings(layoutLevel(shapes[name]))
      expect(crossings.length, crossings.join('\n')).toBeLessThanOrEqual(budget)
    })
  }

  it('keeps crossings low across seeded workflow-like graphs', () => {
    const total = family.reduce((sum, graph) => sum + describeCrossings(layoutLevel(graph)).length, 0)
    expect(total).toBeLessThanOrEqual(FAMILY_CROSSINGS)
  })
})

describe('flowchart layout lanes', () => {
  it('draws a chain as one straight line', () => {
    // The chain's first card is the second's side input.
    const layout = layoutLevel(chain(12))
    expect(new Set(layout.nodes.slice(1).map(center)).size).toBe(1)
    for (const edge of layout.edges.filter((candidate) => !isSideInput(candidate))) {
      expect(new Set(edge.points.map((point) => point.x)).size).toBe(1)
    }
  })

  it('keeps a long edge in one corridor when nothing blocks it', () => {
    const layout = layoutLevel([
      card('start'),
      card('middle_1', ['start']),
      card('middle_2', ['middle_1']),
      card('finish', ['middle_2', 'start']),
    ])
    const edge = layout.edges.find((candidate) => candidate.sourceId === 'start' && candidate.targetId === 'finish')!
    expect(new Set(edge.points.slice(1, -1).map((point) => point.x)).size).toBe(1)
    expect(laneChanges(edge)).toBeLessThanOrEqual(1)
  })

  it('turns an edge passing through ranks only near its ends', () => {
    const innerTurns = (graph: StepDoc[]) => layoutLevel(graph).edges
      .reduce((total, edge) => total + bandTravels(edge).slice(1, -1).filter((travel) => travel !== 0).length, 0)
    expect(innerTurns(ccSpoofGraph())).toBe(0)
    expect(innerTurns(behaviorTransferGraph())).toBe(0)
    expect(family.reduce((total, graph) => total + innerTurns(graph), 0)).toBeLessThanOrEqual(FAMILY_INNER_TURNS)
  })

  it('does not turn edges more often than it has to', () => {
    for (const [name, graph] of [['cc spoof', ccSpoofGraph()], ['behavior transfer', behaviorTransferGraph()]] as const) {
      expect(excessLaneChanges(layoutLevel(graph)), name).toBeLessThanOrEqual(EXCESS_LANE_CHANGES[name])
    }
    const total = family.reduce((sum, graph) => sum + excessLaneChanges(layoutLevel(graph)), 0)
    expect(total).toBeLessThanOrEqual(EXCESS_LANE_CHANGES.family)
  })

  it('does not wobble', () => {
    for (const graph of [...Object.values(shapes), ...family]) {
      for (const edge of layoutLevel(graph).edges) {
        for (const travel of bandTravels(edge)) {
          expect(travel === 0 || Math.abs(travel) >= MIN_TURN, `${edge.sourceId}->${edge.targetId} moves ${travel}px`).toBe(true)
        }
      }
    }
  })
})

describe('flowchart layout spacing', () => {
  it('keeps neighbouring curves apart', () => {
    for (const graph of [ccSpoofGraph(), behaviorTransferGraph(), fan(10), ...family]) {
      const layout = layoutLevel(graph)
      for (const { left, right, distance } of neighbouringCurves(layout)) {
        expect(distance, `${left} and ${right}`).toBeGreaterThanOrEqual(CURVE_CLEARANCE - 0.5)
      }
    }
  })

  it('does not stretch a rank gap for curves far from each other', () => {
    // One long sideways edge, nothing next to it.
    const layout = layoutLevel([card('left'), card('wide_card_in_the_way_of_nothing'), card('far', ['left', 'wide_card_in_the_way_of_nothing'])])
    const gaps = rankGaps(layout)
    expect(Math.max(...gaps)).toBeLessThan(120)
  })

  it('fits the production workflows on one screen at a readable zoom', () => {
    // A 1600 × 1000 graph pane at 80% zoom.
    for (const graph of [ccSpoofGraph(), behaviorTransferGraph()]) {
      const layout = layoutLevel(graph)
      expect(layout.width * 0.8).toBeLessThanOrEqual(1600)
      expect(layout.height * 0.8).toBeLessThanOrEqual(1000)
    }
  })

  it('lays out a large graph quickly', () => {
    const start = performance.now()
    layoutLevel(randomDag(70, 0x5eed))
    for (const graph of family) layoutLevel(graph)
    expect(performance.now() - start).toBeLessThan(3000)
  })
})

// Totals measured on the current layout, as budgets: a change cannot quietly make them worse.
// Lower them when the layout improves.
const FAMILY_CROSSINGS = 100
const FAMILY_INNER_TURNS = 3
const EXCESS_LANE_CHANGES: Record<string, number> = { 'cc spoof': 4, 'behavior transfer': 0, family: 53 }
/** The layout's horizontal grid: the smallest sideways move an edge makes. */
const MIN_TURN = 10

function edgeCount(graph: StepDoc[]): number {
  const visible = new Set(graph.map((node) => node.id))
  return new Set(graph.flatMap((target) => target.deps.filter((id) => visible.has(id)).map((id) => `${id}->${target.id}`))).size
}

function center(node: LayoutNode): number {
  return node.x + node.width / 2
}

function isSideInput(edge: LayoutEdge): boolean {
  return edge.points.length === 2 && edge.points[0].y === edge.points[1].y
}

/** Everything wrong with a layout that no layout may get wrong, one line per problem. */
function violations(layout: FlowchartLayout): string[] {
  return [
    ...cardSpacingViolations(layout),
    ...attachmentViolations(layout),
    ...cardCrossingViolations(layout),
    ...portViolations(layout),
    ...crossingsAtOneCard(layout),
  ]
}

function cardSpacingViolations(layout: FlowchartLayout): string[] {
  const problems: string[] = []
  for (const node of layout.nodes) {
    if (node.x <= 0 || node.y <= 0 || node.x + node.width >= layout.width || node.y + node.height >= layout.height) {
      problems.push(`${node.step.id} is outside the canvas`)
    }
  }
  for (const row of Map.groupBy(layout.nodes, (node) => node.rank).values()) {
    const sorted = [...row].sort((a, b) => a.x - b.x)
    for (let index = 1; index < sorted.length; index += 1) {
      const [left, right] = [sorted[index - 1], sorted[index]]
      if (right.y !== left.y) problems.push(`${left.step.id} and ${right.step.id} share a rank but not a top`)
      // The closest two cards come is a side input beside its consumer.
      if (right.x - left.x - left.width < SIDE_INPUT_GAP) problems.push(`${left.step.id} and ${right.step.id} are too close`)
    }
  }
  return problems
}

/** Edges leave their source's bottom and enter their target's top, running downward. */
function attachmentViolations(layout: FlowchartLayout): string[] {
  const problems: string[] = []
  for (const edge of layout.edges) {
    const name = `${edge.sourceId}->${edge.targetId}`
    const source = layout.byId.get(edge.sourceId)!
    const target = layout.byId.get(edge.targetId)!
    const start = edge.points[0]
    const end = edge.points.at(-1)!
    if (isSideInput(edge)) {
      if (start.x !== source.x + source.width || end.x !== target.x || start.y !== target.y + NODE_H / 2 || source.y !== target.y) {
        problems.push(`${name} does not join the side input to its consumer`)
      }
      continue
    }
    if (target.rank <= source.rank) problems.push(`${name} does not run down the ranks`)
    if (start.y !== source.y + source.height || start.x <= source.x || start.x >= source.x + source.width) problems.push(`${name} does not leave ${edge.sourceId}'s bottom`)
    if (end.y !== target.y || end.x <= target.x || end.x >= target.x + target.width) problems.push(`${name} does not enter ${edge.targetId}'s top`)
    if (edge.renderPoints.some((point, index) => index > 0 && point.y < edge.renderPoints[index - 1].y)) problems.push(`${name} runs upward`)
  }
  return problems
}

function cardCrossingViolations(layout: FlowchartLayout): string[] {
  const problems = new Set<string>()
  for (const edge of layout.edges) {
    const points = edge.renderPoints
    for (let index = 0; index < points.length - 1; index += 1) {
      for (let sample = 0; sample <= 4; sample += 1) {
        const point = interpolate(points[index], points[index + 1], sample / 4)
        for (const node of layout.nodes) {
          if (node.step.id === edge.sourceId || node.step.id === edge.targetId) continue
          if (point.x > node.x - 1 && point.x < node.x + node.width + 1 && point.y > node.y - 1 && point.y < node.y + node.height + 1) {
            problems.add(`${edge.sourceId}->${edge.targetId} crosses ${node.step.id}`)
          }
        }
      }
    }
  }
  return [...problems]
}

function portViolations(layout: FlowchartLayout): string[] {
  const ports = new Map<string, number[]>()
  for (const edge of layout.edges) {
    if (isSideInput(edge)) continue
    ports.set(`${edge.sourceId}'s bottom`, [...(ports.get(`${edge.sourceId}'s bottom`) ?? []), edge.points[0].x])
    ports.set(`${edge.targetId}'s top`, [...(ports.get(`${edge.targetId}'s top`) ?? []), edge.points.at(-1)!.x])
  }
  return [...ports].flatMap(([side, xs]) => {
    const sorted = [...xs].sort((a, b) => a - b)
    return sorted.some((x, index) => index > 0 && x - sorted[index - 1] < EDGE_GAP) ? [`ports on ${side} are crowded`] : []
  })
}

/** Edges leaving one card, or entering one, never cross in the rank gap next to it. */
function crossingsAtOneCard(layout: FlowchartLayout): string[] {
  return crossingPairs(layout)
    .filter(({ left, right, y }) =>
      (left.sourceId === right.sourceId && y <= left.points[1].y) ||
      (left.targetId === right.targetId && y >= left.points.at(-2)!.y))
    .map(({ left, right }) => `${left.sourceId}->${left.targetId} × ${right.sourceId}->${right.targetId} beside their card`)
}

/** x of a downward polyline at height y, which must be within it. */
function xAt(points: LayoutPoint[], y: number): number {
  let low = 0
  let high = points.length - 1
  while (high - low > 1) {
    const middle = (low + high) >> 1
    if (points[middle].y <= y) low = middle
    else high = middle
  }
  const [top, bottom] = [points[low], points[high]]
  return bottom.y === top.y ? top.x : top.x + (bottom.x - top.x) * (y - top.y) / (bottom.y - top.y)
}

/**
 * Pairs of routes that cross, once per crossing, with the height of the crossing: where the sign
 * of their horizontal difference changes along the heights they share. Sampling both routes at
 * every corner of either catches every crossing of the two polylines.
 */
function crossingPairs(layout: FlowchartLayout): Array<{ left: LayoutEdge; right: LayoutEdge; y: number }> {
  const routes = layout.edges.filter((edge) => !isSideInput(edge)).map((edge) => ({
    edge,
    points: edge.renderPoints,
    minX: Math.min(...edge.renderPoints.map((point) => point.x)),
    maxX: Math.max(...edge.renderPoints.map((point) => point.x)),
  }))
  const pairs: Array<{ left: LayoutEdge; right: LayoutEdge; y: number }> = []
  for (let i = 0; i < routes.length; i += 1) {
    for (let j = i + 1; j < routes.length; j += 1) {
      const [left, right] = [routes[i], routes[j]]
      const top = Math.max(left.points[0].y, right.points[0].y)
      const bottom = Math.min(left.points.at(-1)!.y, right.points.at(-1)!.y)
      if (top >= bottom || left.maxX < right.minX || right.maxX < left.minX) continue
      const ys = [...new Set([...left.points, ...right.points].map((point) => point.y).filter((y) => y >= top && y <= bottom))]
        .sort((a, b) => a - b)
      let sign = 0
      for (const y of ys) {
        const difference = xAt(left.points, y) - xAt(right.points, y)
        if (Math.abs(difference) < 0.01) continue
        if (sign !== 0 && Math.sign(difference) !== sign) pairs.push({ left: left.edge, right: right.edge, y })
        sign = Math.sign(difference)
      }
    }
  }
  return pairs
}

function describeCrossings(layout: FlowchartLayout): string[] {
  return crossingPairs(layout).map(({ left, right }) => `${left.sourceId}->${left.targetId} × ${right.sourceId}->${right.targetId}`)
}

/** The sideways travel across each rank gap an edge crosses, top to bottom. */
function bandTravels(edge: LayoutEdge): number[] {
  if (isSideInput(edge)) return []
  const travels: number[] = []
  for (let index = 0; index < edge.points.length - 1; index += 2) travels.push(edge.points[index + 1].x - edge.points[index].x)
  return travels
}

function laneChanges(edge: LayoutEdge): number {
  return bandTravels(edge).filter((travel) => travel !== 0).length
}

/** Turns beyond the one an edge needs when its ports are not in line. */
function excessLaneChanges(layout: FlowchartLayout): number {
  return layout.edges.reduce((total, edge) => {
    if (isSideInput(edge)) return total
    const needed = edge.points[0].x === edge.points.at(-1)!.x ? 0 : 1
    return total + laneChanges(edge) - needed
  }, 0)
}

/**
 * Within each rank gap, the closest approach of each pair of curves adjacent at the gap's top
 * that do not cross there.
 */
function neighbouringCurves(layout: FlowchartLayout): Array<{ left: string; right: string; distance: number }> {
  const curves = layout.edges.flatMap((edge) => {
    if (isSideInput(edge)) return []
    return bandTravels(edge).map((_, band) => {
      const from = edge.points[band * 2]
      const to = edge.points[band * 2 + 1]
      return { name: `${edge.sourceId}->${edge.targetId}`, from, to, samples: edge.renderPoints.filter((point) => point.y >= from.y && point.y <= to.y) }
    })
  })
  const result: Array<{ left: string; right: string; distance: number }> = []
  for (const band of Map.groupBy(curves, (curve) => curve.from.y).values()) {
    const sorted = [...band].sort((a, b) => a.from.x - b.from.x)
    for (let index = 1; index < sorted.length; index += 1) {
      const [left, right] = [sorted[index - 1], sorted[index]]
      if (right.to.x <= left.to.x) continue
      result.push({ left: left.name, right: right.name, distance: polylineDistance(left.samples, right.samples) })
    }
  }
  return result
}

function rankGaps(layout: FlowchartLayout): number[] {
  const bottoms = new Map<number, number>()
  for (const node of layout.nodes) bottoms.set(node.y, Math.max(bottoms.get(node.y) ?? 0, node.y + node.height))
  const tops = [...bottoms.keys()].sort((a, b) => a - b)
  return tops.slice(1).map((top, index) => top - bottoms.get(tops[index])!)
}

function polylineDistance(left: LayoutPoint[], right: LayoutPoint[]): number {
  let distance = Number.POSITIVE_INFINITY
  for (let i = 0; i < left.length - 1; i += 1) {
    for (let j = 0; j < right.length - 1; j += 1) {
      distance = Math.min(
        distance,
        pointSegmentDistance(left[i], right[j], right[j + 1]),
        pointSegmentDistance(left[i + 1], right[j], right[j + 1]),
        pointSegmentDistance(right[j], left[i], left[i + 1]),
        pointSegmentDistance(right[j + 1], left[i], left[i + 1]),
      )
    }
  }
  return distance
}

function pointSegmentDistance(point: LayoutPoint, start: LayoutPoint, end: LayoutPoint): number {
  const dx = end.x - start.x
  const dy = end.y - start.y
  const lengthSquared = dx * dx + dy * dy
  const amount = lengthSquared === 0 ? 0 : Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared))
  return Math.hypot(point.x - (start.x + dx * amount), point.y - (start.y + dy * amount))
}

function interpolate(start: LayoutPoint, end: LayoutPoint, amount: number): LayoutPoint {
  return { x: start.x + (end.x - start.x) * amount, y: start.y + (end.y - start.y) * amount }
}
