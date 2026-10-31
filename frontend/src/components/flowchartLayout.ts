/**
 * The flowchart's layout of one level's card DAG, top to bottom.
 *
 * What a good layout is here, in rough priority order:
 *
 * - No edge crosses a card, and no card overlaps another.
 * - As few edge crossings as the graph allows; routing never adds one the ordering did not have,
 *   and two edges of the same card never cross each other.
 * - No edge changes lanes more than it has to: an edge between two ranks is one S-curve (or a
 *   straight line), an edge passing through ranks runs straight down one corridor and turns only
 *   near its ends, and nothing is off by a few pixels (a wobble reads as a mistake, not a turn).
 * - Breathing room without sprawl: cards and edges keep clear gaps, curves side by side stay
 *   visibly apart, but a typical workflow still fits on one screen at a readable zoom.
 * - Deterministic: the same graph always lays out the same way, so a live update that does not
 *   change the graph's shape never moves anything.
 * - Simple enough to reason about: a few standard phases, each with one job, rather than repairs
 *   layered on repairs. Fast enough to run on the main thread.
 *
 * The phases (a Sugiyama-style layered layout, as in Gansner et al., "A Technique for Drawing
 * Directed Graphs", 1993):
 *
 * 1. Ranks: longest path from the inputs, then every card with more consumers than inputs moves
 *    down to just above its first consumer, which shortens more edges than it lengthens.
 * 2. Slots: a rank is a row of slots, one per card and one per edge passing through the rank on
 *    its way to a lower one. An input read by exactly one card shares a slot with it, to its left.
 * 3. Order: barycenter sweeps with adjacent transpositions, from several starting orders, keeping
 *    the order with the fewest crossings between neighbouring ranks.
 * 4. Ports: an edge leaves its card's bottom and enters its target's top at a port ordered by
 *    where the edge goes, so edges of one card never cross.
 * 5. Positions: the horizontal positions that minimize the weighted total sideways travel of the
 *    edges, subject to the order and the gaps between slots (see assignPositions).
 * 6. Routes: between two ranks every edge is the same vertical S-curve stretched between its
 *    ends, and two such curves cross only when their ends are in opposite orders; within a rank a
 *    passing edge runs straight down its slot. So the crossings drawn are exactly the crossings
 *    the order has. Each rank gap is as short as keeping its curves apart allows (see rankTops).
 *
 * A card's dims stack down it, so cards differ in height (nodeHeight). The cards of a rank share
 * their top, the rank is as tall as its tallest card, and an edge leaving a shorter card runs
 * straight down to the rank's bottom before it curves, so every curve spans a whole rank gap.
 *
 * Weakly connected components are laid out separately and placed side by side, in the order of
 * their first card in the level.
 */
import { KIND_ICON_SIZE } from '../lib/kindIcons'
import { networkSimplex, type SimplexEdge } from '../lib/networkSimplex'
import type { StepDoc } from '../types'

/** The height of a card with at most one dim. */
export const NODE_H = 58
/** Where a card's first dim box starts, and the step from each dim box to the next below it. */
export const DIM_TOP = 32
export const DIM_PITCH = 24
/**
 * Every horizontal gap, port offset, and half card width is a multiple of this, and so every
 * horizontal position (see assignPositions): an edge moves sideways by at least GRID or not at
 * all, never by a few pixels.
 */
const GRID = 10
/** The horizontal room between two cards of a rank. */
const NODE_GAP = 60
/** The horizontal room between two edges passing through a rank, and between two ports of a card. */
export const EDGE_GAP = 20
/** The horizontal room between an edge passing through a rank and a card beside it. */
const EDGE_NODE_GAP = 30
/** The room between a side input and the card it feeds. Not on the grid: the side input, which
 *  has no ports, is widened so that it and this gap together are (see cardWidths). */
export const SIDE_INPUT_GAP = 13
const COMPONENT_GAP = 96
const PAD = 48
/** The room between a card's corner and its outermost port. */
const PORT_MARGIN = 24
// Cards are deliberately compact; these gaps preserve enough vertical run for clean S-curves.
const RANK_GAP_MIN = 92
const RANK_GAP_MAX = 220
/** Neighbouring curves across a rank gap are kept this far apart, measured across them. */
export const CURVE_CLEARANCE = 8
/** The steepest a curve gets, as sideways over downward travel at its middle. */
const MAX_CURVE_SLOPE = 5
const CURVE_SAMPLES = 16

export interface LayoutPoint {
  x: number
  y: number
}

export interface LayoutNode {
  step: StepDoc
  /** The card's left edge. */
  x: number
  /** The card's top edge. */
  y: number
  width: number
  height: number
  rank: number
}

export interface LayoutEdge {
  sourceId: string
  targetId: string
  /**
   * The route's corners: the source port (then the bottom of its rank, when the source is shorter
   * than the rank), the top and bottom of every rank the edge passes through, and the target port. Consecutive corners across a rank gap are joined by an S-curve,
   * within a rank by a vertical line. A side input's edge is one horizontal segment.
   */
  points: LayoutPoint[]
  /** The rank each of `points` lies in. */
  pointRanks: number[]
  /** The drawn route as a polyline, the curves sampled. */
  renderPoints: LayoutPoint[]
  /** Whether a longer path joins the same two cards, so the edge adds no ordering of its own. */
  secondary: boolean
}

/**
 * One rank's band: its vertical extent, and the horizontal room its cards take, margins included.
 * Edges passing through the rank are left out: a rank holding one card and many passing edges is
 * as wide as that card, so the flowchart's parallax pan keeps it on screen (see flowchartParallax).
 */
export interface LayoutRank {
  top: number
  height: number
  left: number
  right: number
  /** The extent of everything in the rank, passing edges included, margins not. */
  slotsLeft: number
  slotsRight: number
}

export interface FlowchartLayout {
  nodes: LayoutNode[]
  byId: ReadonlyMap<string, LayoutNode>
  edges: LayoutEdge[]
  ranks: LayoutRank[]
  width: number
  height: number
}

interface Edge {
  sourceId: string
  targetId: string
  secondary: boolean
  /** The segments the edge is drawn as, top to bottom. */
  segments: Segment[]
}

/** One rank's place for a card (with its side input, if it has one) or a passing edge. */
interface Slot {
  rank: number
  width: number
  /** Null for an edge passing through the rank. */
  card: StepDoc | null
  sideInput: StepDoc | null
  /** From the slot's centre to the centre of the card its edges attach to. */
  anchor: number
  /** Segments from the rank above, and to the rank below. */
  up: Segment[]
  down: Segment[]
  /** Index within the rank. */
  pos: number
  /** Horizontal centre. */
  x: number
}

/** An edge's piece between two adjacent ranks. Ports are offsets from the slots' centres. */
interface Segment {
  edge: Edge
  upper: Slot
  lower: Slot
  upperPort: number
  lowerPort: number
}

export function layoutLevel(level: StepDoc[]): FlowchartLayout {
  if (level.length === 0) return { nodes: [], byId: new Map(), edges: [], ranks: [], width: 1, height: 1 }

  const edges = uniqueEdges(level)
  const sideInputs = sideInputsOf(level, edges)
  const sideInputIds = new Set([...sideInputs.values()].map((step) => step.id))
  const cards = level.filter((step) => !sideInputIds.has(step.id))
  const routed = edges.filter((edge) => !sideInputIds.has(edge.sourceId))
  const widths = cardWidths(level, routed, sideInputIds)

  let left = PAD
  const ranks: Slot[][] = []
  for (const component of components(cards, routed)) {
    const componentRanks = layoutComponent(component.cards, component.edges, sideInputs, widths)
    const slots = componentRanks.flat()
    const shift = left - Math.min(...slots.map((slot) => slot.x - slot.width / 2))
    for (const slot of slots) slot.x += shift
    left = Math.max(...slots.map((slot) => slot.x + slot.width / 2)) + COMPONENT_GAP
    for (const [rank, row] of componentRanks.entries()) ranks[rank] = [...(ranks[rank] ?? []), ...row]
  }

  const heights = rankHeights(ranks)
  const tops = rankTops(ranks, heights)
  const nodes: LayoutNode[] = []
  for (const slot of ranks.flat()) {
    if (!slot.card) continue
    const y = tops[slot.rank]
    let x = slot.x - slot.width / 2
    if (slot.sideInput) {
      nodes.push({ step: slot.sideInput, x, y, width: widths.get(slot.sideInput.id)!, height: nodeHeight(slot.sideInput), rank: slot.rank })
      x += widths.get(slot.sideInput.id)! + SIDE_INPUT_GAP
    }
    nodes.push({ step: slot.card, x, y, width: widths.get(slot.card.id)!, height: nodeHeight(slot.card), rank: slot.rank })
  }
  const byId = new Map(nodes.map((node) => [node.step.id, node]))

  const drawn = edges.map((edge): LayoutEdge => {
    if (sideInputIds.has(edge.sourceId)) {
      const source = byId.get(edge.sourceId)!
      const target = byId.get(edge.targetId)!
      const y = source.y + NODE_H / 2
      const points = [{ x: source.x + source.width, y }, { x: target.x, y }]
      const pointRanks = [source.rank, source.rank]
      return { ...edgeIds(edge), points, pointRanks, renderPoints: points, secondary: false }
    }
    return { ...edgeIds(edge), ...route(edge, tops, heights), secondary: edge.secondary }
  })
  // Secondary edges are drawn first, beneath the others.
  drawn.sort((a, b) => Number(b.secondary) - Number(a.secondary))

  return {
    nodes,
    byId,
    edges: drawn,
    ranks: ranks.map((row, rank) => {
      const cardSlots = row.filter((slot) => slot.card !== null)
      const spanned = cardSlots.length > 0 ? cardSlots : row
      return {
        top: tops[rank],
        height: heights[rank],
        left: Math.min(...spanned.map((slot) => slot.x - slot.width / 2)) - PAD,
        right: Math.max(...spanned.map((slot) => slot.x + slot.width / 2)) + PAD,
        slotsLeft: Math.min(...row.map((slot) => slot.x - slot.width / 2)),
        slotsRight: Math.max(...row.map((slot) => slot.x + slot.width / 2)),
      }
    }),
    width: left - COMPONENT_GAP + PAD,
    height: tops.at(-1)! + heights.at(-1)! + PAD,
  }
}

function edgeIds(edge: Edge) {
  return { sourceId: edge.sourceId, targetId: edge.targetId }
}

/**
 * Inputs with exactly one consumer are drawn beside it, as that card's own local input. A card
 * takes at most one: several lined up beside one card would need connectors that overlap.
 */
function sideInputsOf(level: StepDoc[], edges: Edge[]): Map<string, StepDoc> {
  const incoming = new Set(edges.map((edge) => edge.targetId))
  const consumers = new Map<string, string[]>()
  for (const edge of edges) consumers.set(edge.sourceId, [...(consumers.get(edge.sourceId) ?? []), edge.targetId])
  const result = new Map<string, StepDoc>()
  for (const step of level) {
    const targets = consumers.get(step.id) ?? []
    if (incoming.has(step.id) || targets.length !== 1 || result.has(targets[0])) continue
    result.set(targets[0], step)
  }
  return result
}

/**
 * A card is wide enough for its text and for its ports on either side, rounded up so its half
 * width is on the grid. A side input has no ports, so it is rounded instead so that it and the gap
 * beside it are, which keeps its consumer's ports on the grid.
 */
function cardWidths(level: StepDoc[], routed: Edge[], sideInputIds: ReadonlySet<string>): Map<string, number> {
  const inPorts = new Map<string, number>()
  const outPorts = new Map<string, number>()
  for (const edge of routed) {
    outPorts.set(edge.sourceId, (outPorts.get(edge.sourceId) ?? 0) + 1)
    inPorts.set(edge.targetId, (inPorts.get(edge.targetId) ?? 0) + 1)
  }
  return new Map(level.map((step) => {
    const ports = Math.max(inPorts.get(step.id) ?? 0, outPorts.get(step.id) ?? 0)
    const needed = Math.max(measureNode(step), (ports - 1) * EDGE_GAP + 2 * PORT_MARGIN)
    const onGrid = (width: number) => 2 * GRID * Math.ceil(width / (2 * GRID))
    return [step.id, sideInputIds.has(step.id) ? onGrid(needed + SIDE_INPUT_GAP) - SIDE_INPUT_GAP : onGrid(needed)]
  }))
}

/** Weakly connected components, each in level order, ordered by their first card. */
function components(cards: StepDoc[], edges: Edge[]): Array<{ cards: StepDoc[]; edges: Edge[] }> {
  const parent = new Map(cards.map((card) => [card.id, card.id]))
  const find = (id: string): string => {
    const up = parent.get(id)!
    if (up === id) return id
    const root = find(up)
    parent.set(id, root)
    return root
  }
  for (const edge of edges) parent.set(find(edge.sourceId), find(edge.targetId))
  const groups = new Map<string, { cards: StepDoc[]; edges: Edge[] }>()
  for (const card of cards) {
    const root = find(card.id)
    if (!groups.has(root)) groups.set(root, { cards: [], edges: [] })
    groups.get(root)!.cards.push(card)
  }
  for (const edge of edges) groups.get(find(edge.sourceId))!.edges.push(edge)
  return [...groups.values()]
}

function layoutComponent(
  cards: StepDoc[],
  edges: Edge[],
  sideInputs: ReadonlyMap<string, StepDoc>,
  widths: ReadonlyMap<string, number>,
): Slot[][] {
  const rankOf = assignRanks(cards, edges)
  const ranks: Slot[][] = Array.from({ length: Math.max(...rankOf.values()) + 1 }, () => [])
  const slot = (rank: number, card: StepDoc | null): Slot => {
    const sideInput = card ? sideInputs.get(card.id) ?? null : null
    const sideWidth = sideInput ? widths.get(sideInput.id)! + SIDE_INPUT_GAP : 0
    const created: Slot = {
      rank,
      width: card ? sideWidth + widths.get(card.id)! : 0,
      card,
      sideInput,
      anchor: sideWidth / 2,
      up: [],
      down: [],
      pos: ranks[rank].length,
      x: 0,
    }
    ranks[rank].push(created)
    return created
  }
  const cardSlots = new Map(cards.map((card) => [card.id, slot(rankOf.get(card.id)!, card)]))
  for (const edge of edges) {
    const target = cardSlots.get(edge.targetId)!
    let upper = cardSlots.get(edge.sourceId)!
    while (upper !== target) {
      const lower = upper.rank + 1 === target.rank ? target : slot(upper.rank + 1, null)
      const segment: Segment = { edge, upper, lower, upperPort: 0, lowerPort: 0 }
      edge.segments.push(segment)
      upper.down.push(segment)
      lower.up.push(segment)
      upper = lower
    }
  }
  orderRanks(ranks)
  assignPorts(ranks)
  assignPositions(ranks)
  return ranks
}

function assignRanks(cards: StepDoc[], edges: Edge[]): Map<string, number> {
  const inputs = new Map(cards.map((card) => [card.id, [] as string[]]))
  const consumers = new Map(cards.map((card) => [card.id, [] as string[]]))
  for (const edge of edges) {
    inputs.get(edge.targetId)!.push(edge.sourceId)
    consumers.get(edge.sourceId)!.push(edge.targetId)
  }
  const order = topologicalOrder(cards, inputs, consumers)
  const rank = new Map<string, number>()
  for (const id of order) rank.set(id, Math.max(-1, ...inputs.get(id)!.map((input) => rank.get(input)!)) + 1)
  // In reverse topological order every consumer's rank is final when its inputs are reached.
  for (const id of [...order].reverse()) {
    const after = consumers.get(id)!
    if (after.length > inputs.get(id)!.length) rank.set(id, Math.min(...after.map((consumer) => rank.get(consumer)!)) - 1)
  }
  const lowest = Math.min(...rank.values())
  for (const [id, value] of rank) rank.set(id, value - lowest)
  return rank
}

/** Kahn's algorithm, taking ready cards in level order. */
function topologicalOrder(
  cards: StepDoc[],
  inputs: ReadonlyMap<string, string[]>,
  consumers: ReadonlyMap<string, string[]>,
): string[] {
  const remaining = new Map(cards.map((card) => [card.id, inputs.get(card.id)!.length]))
  const ready = cards.filter((card) => remaining.get(card.id) === 0).map((card) => card.id)
  const order: string[] = []
  while (ready.length > 0) {
    const id = ready.shift()!
    order.push(id)
    for (const consumer of consumers.get(id)!) {
      const left = remaining.get(consumer)! - 1
      remaining.set(consumer, left)
      if (left === 0) ready.push(consumer)
    }
  }
  if (order.length !== cards.length) throw new Error('the flowchart\'s dependency graph has a cycle')
  return order
}

const ORDER_STARTS = 8
const MAX_ORDER_SWEEPS = 32
const MAX_STALE_SWEEPS = 6

/**
 * Sweeps settle into a local optimum that depends on where they start, so they start from the
 * level order and from seeded shuffles of it, and the order with the fewest crossings wins. The
 * shuffles are seeded, so a graph always lays out the same way.
 */
function orderRanks(ranks: Slot[][]) {
  const random = seededRandom(1)
  let best = ranks.map((row) => [...row])
  let fewest = Number.POSITIVE_INFINITY
  for (let start = 0; start < ORDER_STARTS && fewest > 0; start += 1) {
    if (start > 0) for (const row of ranks) shuffle(row, random)
    const crossings = sweep(ranks)
    if (crossings < fewest) {
      best = ranks.map((row) => [...row])
      fewest = crossings
    }
  }
  restore(ranks, best)
}

/**
 * Alternate downward and upward barycenter sweeps, each followed by transpositions, until they
 * stop finding fewer crossings; leaves the best order found and returns its crossings.
 */
function sweep(ranks: Slot[][]): number {
  let best = ranks.map((row) => [...row])
  let fewest = countCrossings(ranks)
  for (let sweep = 0, stale = 0; sweep < MAX_ORDER_SWEEPS && fewest > 0 && stale < MAX_STALE_SWEEPS; sweep += 1) {
    if (sweep % 2 === 0) for (const row of ranks.slice(1)) reorder(row, 'up')
    else for (const row of ranks.slice(0, -1).reverse()) reorder(row, 'down')
    transpose(ranks)
    const crossings = countCrossings(ranks)
    if (crossings < fewest) {
      best = ranks.map((row) => [...row])
      fewest = crossings
      stale = 0
    } else {
      stale += 1
    }
  }
  restore(ranks, best)
  return fewest
}

function restore(ranks: Slot[][], order: Slot[][]) {
  for (const [rank, row] of order.entries()) {
    ranks[rank] = [...row]
    renumber(ranks[rank])
  }
}

function seededRandom(seed: number): () => number {
  let state = seed
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0
    return state / 0x100000000
  }
}

function shuffle(row: Slot[], random: () => number) {
  for (let index = row.length - 1; index > 0; index -= 1) {
    const other = Math.floor(random() * (index + 1))
    ;[row[index], row[other]] = [row[other], row[index]]
  }
  renumber(row)
}

/**
 * Sort a rank by the mean position of each slot's neighbours on one side. Slots without neighbours
 * there keep their place; the sort is stable, so ties keep the current order.
 */
function reorder(row: Slot[], side: 'up' | 'down') {
  const neighbour = (segment: Segment) => (side === 'up' ? segment.upper : segment.lower)
  const barycenter = new Map<Slot, number>()
  for (const slot of row) {
    const segments = slot[side]
    if (segments.length > 0) barycenter.set(slot, segments.reduce((total, segment) => total + neighbour(segment).pos, 0) / segments.length)
  }
  const sorted = row.filter((slot) => barycenter.has(slot)).sort((a, b) => barycenter.get(a)! - barycenter.get(b)!)
  let next = 0
  for (const [index, slot] of row.entries()) if (barycenter.has(slot)) row[index] = sorted[next++]
  renumber(row)
}

/** Swap neighbouring slots while that removes crossings. */
function transpose(ranks: Slot[][]) {
  for (let improved = true, pass = 0; improved && pass < 16; pass += 1) {
    improved = false
    for (const row of ranks) {
      for (let index = 0; index < row.length - 1; index += 1) {
        const left = row[index]
        const right = row[index + 1]
        if (pairCrossings(left, right) <= pairCrossings(right, left)) continue
        row[index] = right
        row[index + 1] = left
        right.pos = index
        left.pos = index + 1
        improved = true
      }
    }
  }
}

/** Crossings between the edges of two slots of a rank, with `left` placed before `right`. */
function pairCrossings(left: Slot, right: Slot): number {
  let crossings = 0
  for (const [side, end] of [['up', 'upper'], ['down', 'lower']] as const) {
    for (const a of left[side]) for (const b of right[side]) if (a[end].pos > b[end].pos) crossings += 1
  }
  return crossings
}

/**
 * Crossings between every pair of adjacent ranks: with segments sorted by their upper ends, two
 * cross exactly when their lower ends are inverted, and a Fenwick tree counts the inversions.
 */
function countCrossings(ranks: Slot[][]): number {
  let crossings = 0
  for (const [rank, row] of ranks.slice(0, -1).entries()) {
    const size = ranks[rank + 1].length
    const lower = row.flatMap((slot) => slot.down)
      .sort((a, b) => a.upper.pos - b.upper.pos || a.lower.pos - b.lower.pos)
      .map((segment) => segment.lower.pos)
    const tree = new Array<number>(size + 1).fill(0)
    for (const [seen, pos] of lower.entries()) {
      // Segments seen so far whose lower end is at or left of this one's; the rest cross it.
      let atOrLeft = 0
      for (let index = pos + 1; index > 0; index -= index & -index) atOrLeft += tree[index]
      crossings += seen - atOrLeft
      for (let index = pos + 1; index <= size; index += index & -index) tree[index] += 1
    }
  }
  return crossings
}

function renumber(row: Slot[]) {
  for (const [index, slot] of row.entries()) slot.pos = index
}

/** A card's ports on each side are EDGE_GAP apart around its centre, in the order of where they lead. */
function assignPorts(ranks: Slot[][]) {
  for (const slot of ranks.flat()) {
    const down = [...slot.down].sort((a, b) => a.lower.pos - b.lower.pos)
    for (const [index, segment] of down.entries()) segment.upperPort = slot.anchor + (index - (down.length - 1) / 2) * EDGE_GAP
    const up = [...slot.up].sort((a, b) => a.upper.pos - b.upper.pos)
    for (const [index, segment] of up.entries()) segment.lowerPort = slot.anchor + (index - (up.length - 1) / 2) * EDGE_GAP
  }
}

/**
 * Horizontal centres minimizing the weighted sideways travel of every segment, |upper port −
 * lower port|, subject to the order and gaps within each rank. Measuring travel linearly (rather
 * than squared) makes a segment exactly vertical wherever the gaps allow, so an edge changes lanes
 * only where it has to, and never by a few pixels. The problem is solved exactly as Gansner et al.
 * do: each segment gets an auxiliary node that sits at the smaller of its two port positions, so
 * minimizing the auxiliary edges' total length minimizes the travel, and network simplex finds the
 * optimum. Its values are sums of the edges' minimum lengths, so they are on the grid.
 */
function assignPositions(ranks: Slot[][]) {
  const slots = ranks.flat()
  const id = new Map(slots.map((slot, index) => [slot, index]))
  const edges: SimplexEdge[] = []
  for (const row of ranks) {
    for (let index = 1; index < row.length; index += 1) {
      edges.push({ v: id.get(row[index - 1])!, w: id.get(row[index])!, minlen: separation(row[index - 1], row[index]), weight: 0 })
    }
  }
  let next = slots.length
  for (const slot of slots) {
    for (const segment of slot.down) {
      const weight = segmentWeight(segment)
      edges.push({ v: next, w: id.get(segment.upper)!, minlen: -segment.upperPort, weight })
      edges.push({ v: next, w: id.get(segment.lower)!, minlen: -segment.lowerPort, weight })
      next += 1
    }
  }
  if (edges.some((edge) => edge.minlen % GRID !== 0)) throw new Error('flowchart gaps and ports must be on the grid')
  const x = networkSimplex(next, edges)
  for (const [index, slot] of slots.entries()) slot.x = x[index]
}

/**
 * Edges passing through ranks are pulled straight hardest (Gansner et al.'s Ω), so they change
 * lanes only at their ends. Their last segment outweighs their first: equal weights would make
 * every corridor between the two ports equally good, and the corridor could settle halfway,
 * turning twice; this way it lines up with the card it feeds and turns once, below its source.
 */
function segmentWeight(segment: Segment): number {
  if (segment.upper.card === null) return segment.lower.card === null ? 8 : 3
  return segment.lower.card === null ? 2 : 1
}

function separation(left: Slot, right: Slot): number {
  const passing = Number(left.card === null) + Number(right.card === null)
  return left.width / 2 + [NODE_GAP, EDGE_NODE_GAP, EDGE_GAP][passing] + right.width / 2
}

/** The height of every rank: its tallest card's. */
function rankHeights(ranks: Slot[][]): number[] {
  return ranks.map((row) =>
    Math.max(NODE_H, ...row.flatMap((slot) => [slot.card, slot.sideInput]).flatMap((step) => (step ? [nodeHeight(step)] : []))))
}

/**
 * The top of every rank. A gap is tall enough that neighbouring curves across it stay
 * CURVE_CLEARANCE apart where they are steepest (up to RANK_GAP_MAX), and no curve is flatter
 * than MAX_CURVE_SLOPE.
 */
function rankTops(ranks: Slot[][], heights: readonly number[]): number[] {
  const tops = [PAD]
  for (const [rank, row] of ranks.slice(0, -1).entries()) {
    const curves = row.flatMap((slot) => slot.down)
      .map((segment) => ({ from: segment.upper.x + segment.upperPort, to: segment.lower.x + segment.lowerPort }))
      .sort((a, b) => a.from - b.from)
    // A curve's steepest slope, at its middle, is 1.5 × its sideways travel / the gap.
    let gap = Math.max(RANK_GAP_MIN, ...curves.map(({ from, to }) => 1.5 * Math.abs(to - from) / MAX_CURVE_SLOPE))
    for (let index = 1; index < curves.length; index += 1) {
      const left = curves[index - 1]
      const right = curves[index]
      // Curves that cross need nothing. Otherwise they are closest across their middles, where
      // they are steepest (at their ends they run vertically, as far apart as their ports).
      if (right.to - left.to <= 0) continue
      const apart = (right.from - left.from + right.to - left.to) / 2
      const travel = Math.max(Math.abs(left.to - left.from), Math.abs(right.to - right.from))
      const steepest = apart > CURVE_CLEARANCE ? Math.sqrt((apart / CURVE_CLEARANCE) ** 2 - 1) : 0
      gap = Math.max(gap, steepest > 0 ? 1.5 * travel / steepest : RANK_GAP_MAX)
    }
    tops.push(tops.at(-1)! + heights[rank] + Math.min(RANK_GAP_MAX, Math.ceil(gap)))
  }
  return tops
}

function route(edge: Edge, tops: number[], heights: readonly number[]): Pick<LayoutEdge, 'points' | 'pointRanks' | 'renderPoints'> {
  const first = edge.segments[0]
  const rank = first.upper.rank
  const start = { x: first.upper.x + first.upperPort, y: tops[rank] + nodeHeight(first.upper.card!) }
  const points = [start]
  const pointRanks = [rank]
  const renderPoints = [start]
  const rankBottom = tops[rank] + heights[rank]
  if (start.y < rankBottom) {
    const below = { x: start.x, y: rankBottom }
    points.push(below)
    pointRanks.push(rank)
    renderPoints.push(below)
  }
  for (const segment of edge.segments) {
    const from = points.at(-1)!
    const to = { x: segment.lower.x + segment.lowerPort, y: tops[segment.lower.rank] }
    for (let sample = 1; sample <= CURVE_SAMPLES; sample += 1) {
      const t = sample / CURVE_SAMPLES
      renderPoints.push({ x: from.x + (to.x - from.x) * t * t * (3 - 2 * t), y: from.y + (to.y - from.y) * t })
    }
    points.push(to)
    pointRanks.push(segment.lower.rank)
    if (segment.lower.card === null) {
      const below = { x: to.x, y: to.y + heights[segment.lower.rank] }
      points.push(below)
      pointRanks.push(segment.lower.rank)
      renderPoints.push(below)
    }
  }
  return { points, pointRanks, renderPoints }
}

/**
 * SVG path data through a route's corners: a straight line between corners in one rank, and an
 * S-curve between corners in different ranks. The flowchart redraws routes from their corners
 * after sliding each rank sideways, so the curves stay S-curves between wherever their ends are.
 */
export function edgePath(points: readonly LayoutPoint[], pointRanks: readonly number[]): string {
  let path = `M ${points[0].x} ${points[0].y}`
  for (let index = 1; index < points.length; index += 1) {
    const from = points[index - 1]
    const to = points[index]
    if (pointRanks[index] === pointRanks[index - 1]) {
      path += ` L ${to.x} ${to.y}`
      continue
    }
    // Control points a third of the way down each keep the curve's height linear in its parameter,
    // so every curve across one gap moves sideways on the same schedule (a smoothstep).
    const third = (to.y - from.y) / 3
    path += ` C ${from.x} ${from.y + third} ${to.x} ${to.y - third} ${to.x} ${to.y}`
  }
  return path
}

export function measureDim(dim: string): number {
  return Math.max(34, dim.length * 6.2 + 16)
}

/** A card's height: its dims stack down it from DIM_TOP, one every DIM_PITCH. */
export function nodeHeight(step: StepDoc): number {
  return NODE_H + Math.max(0, step.dims.length - 1) * DIM_PITCH
}

/** A card's inner margin, left and right. */
export const NODE_PAD = 12
/** Where a card's name starts: after the kind icon. */
export const NAME_X = NODE_PAD + KIND_ICON_SIZE + 6
function measureNode(step: StepDoc): number {
  const head = NAME_X + step.name.length * 7.2 + NODE_PAD
  const dims = step.dims.reduce((widest, dim) => Math.max(widest, measureDim(dim) + 2 * NODE_PAD), 0)
  return Math.max(head, dims)
}

/** One edge per pair of cards, however many arguments connect them. */
function uniqueEdges(level: StepDoc[]): Edge[] {
  const visible = new Set(level.map((step) => step.id))
  const seen = new Set<string>()
  const edges: Edge[] = []
  for (const target of level) {
    for (const sourceId of target.deps) {
      const key = `${sourceId}->${target.id}`
      if (!visible.has(sourceId) || seen.has(key)) continue
      seen.add(key)
      edges.push({ sourceId, targetId: target.id, secondary: false, segments: [] })
    }
  }
  const consumers = new Map<string, string[]>()
  for (const edge of edges) consumers.set(edge.sourceId, [...(consumers.get(edge.sourceId) ?? []), edge.targetId])
  for (const edge of edges) edge.secondary = reachableAvoiding(edge, consumers)
  return edges
}

/** Whether the edge's target is reachable from its source without taking the edge itself. */
function reachableAvoiding(edge: Edge, consumers: ReadonlyMap<string, string[]>): boolean {
  const pending = (consumers.get(edge.sourceId) ?? []).filter((id) => id !== edge.targetId)
  const visited = new Set(pending)
  while (pending.length > 0) {
    const id = pending.pop()!
    if (id === edge.targetId) return true
    for (const next of consumers.get(id) ?? []) {
      if (!visited.has(next)) {
        visited.add(next)
        pending.push(next)
      }
    }
  }
  return false
}
