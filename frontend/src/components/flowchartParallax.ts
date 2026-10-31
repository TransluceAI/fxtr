/**
 * Parallax panning for the flowchart: every rank slides sideways on its own, all driven by one
 * pan fraction, so a long row can be scrolled through at a readable zoom while the short rows
 * above and below it stay on screen.
 *
 * - A rank wider than the viewport scrolls through its whole width as the pan goes from 0 to 1:
 *   at 0 its left end is at the viewport's left edge, at 1 its right end at the right edge.
 * - A rank that fits does not pan. It keeps the place it has in the layout, relative to the
 *   layout's own width (or the viewport's, once the layout is wider than the viewport).
 *
 * Both cases put a rank's left edge at `share × slack` into the frame, where the slack is the
 * frame's width minus the rank's, and the share is the pan for a rank that overflows and the
 * rank's place in the layout for one that fits. A rank exactly as wide as the frame has no slack,
 * so a rank starting or stopping to overflow as the zoom changes never jumps. When the whole
 * layout fits, every rank is where the layout put it: the view is the layout, centred.
 *
 * A rank's width is its cards', so the lanes of edges passing through it beside its cards can lie
 * off screen even when the cards do not. Those lanes are squeezed, in order, into the room between
 * the cards and the viewport's side (and left alone when they already fit there).
 *
 * Sliding a rank as a whole, and squeezing its lanes in order, keeps the order of every edge end
 * within it; two S-curves across a rank gap cross exactly when their ends are in opposite orders,
 * so panning never adds a crossing.
 *
 * Drags and zooms are anchored on the overflowing rank nearest the pointer: it moves exactly with
 * the pointer, and every other rank follows the shared pan.
 */
import type { FlowchartLayout } from './flowchartLayout'

export interface ParallaxView {
  /** How far through each overflowing rank the view is, from 0 (left end) to 1 (right end). */
  pan: number
  /** The screen y of the layout's top. */
  y: number
  scale: number
}

/** The screen span every rank is placed within: the viewport, or the layout centred in it. */
function frame(layout: FlowchartLayout, scale: number, viewportWidth: number) {
  const width = Math.min(viewportWidth, scale * layout.width)
  return { left: (viewportWidth - width) / 2, width }
}

/** How much narrower than the frame the rank is on screen; negative when it overflows. */
function slack(layout: FlowchartLayout, rank: number, scale: number, viewportWidth: number): number {
  const { left, right } = layout.ranks[rank]
  return frame(layout, scale, viewportWidth).width - scale * (right - left)
}

/** The screen x of the rank's left edge. */
function rankLeft(layout: FlowchartLayout, rank: number, view: ParallaxView, viewportWidth: number): number {
  const { left, right } = layout.ranks[rank]
  const room = slack(layout, rank, view.scale, viewportWidth)
  const spare = layout.width - (right - left)
  const share = room < 0 ? view.pan : spare > 0 ? left / spare : 0.5
  return frame(layout, view.scale, viewportWidth).left + share * room
}

/** How near the viewport's side, in screen pixels, a rank's squeezed lanes may come. */
const LANE_MARGIN = 12

/** Where one rank is drawn, in layout units, when the layout is drawn at the view's scale with its
 *  left edge at the viewport's. */
export interface RankPlacement {
  /** How far the rank is slid from its place in the layout. */
  shift: number
  /** Beyond these (the rank's card room), lanes are squeezed towards them by these factors. */
  left: number
  right: number
  leftSqueeze: number
  rightSqueeze: number
}

export function rankPlacements(layout: FlowchartLayout, view: ParallaxView, viewportWidth: number): RankPlacement[] {
  const margin = LANE_MARGIN / view.scale
  return layout.ranks.map((band, rank) => {
    const shift = rankLeft(layout, rank, view, viewportWidth) / view.scale - band.left
    // The room between the cards and each side of the viewport, and the lanes that must fit in it.
    const leftRoom = band.left + shift - margin
    const rightRoom = viewportWidth / view.scale - margin - (band.right + shift)
    const squeeze = (room: number, lanes: number) => (lanes > 0 ? Math.min(1, Math.max(0, room / lanes)) : 1)
    return {
      shift,
      left: band.left,
      right: band.right,
      leftSqueeze: squeeze(leftRoom, band.left - band.slotsLeft),
      rightSqueeze: squeeze(rightRoom, band.slotsRight - band.right),
    }
  })
}

/** Where a point of the rank at layout x is drawn. */
export function placeX(placement: RankPlacement, x: number): number {
  const { shift, left, right, leftSqueeze, rightSqueeze } = placement
  if (x < left) return left - (left - x) * leftSqueeze + shift
  if (x > right) return right + (x - right) * rightSqueeze + shift
  return x + shift
}

/** The overflowing rank nearest the screen height `pointerY`, or null when every rank fits. */
function anchorRank(layout: FlowchartLayout, view: ParallaxView, scale: number, viewportWidth: number, pointerY: number): number | null {
  const y = (pointerY - view.y) / view.scale
  let nearest: number | null = null
  let nearestDistance = Number.POSITIVE_INFINITY
  for (const [rank, band] of layout.ranks.entries()) {
    if (slack(layout, rank, scale, viewportWidth) >= 0) continue
    const distance = Math.max(0, band.top - y, y - (band.top + band.height))
    if (distance < nearestDistance) {
      nearest = rank
      nearestDistance = distance
    }
  }
  return nearest
}

const clampPan = (pan: number) => Math.min(1, Math.max(0, pan))

/** The view after dragging by (dx, dy) screen pixels with the pointer at screen height `pointerY`. */
export function panBy(
  layout: FlowchartLayout,
  view: ParallaxView,
  viewportWidth: number,
  pointerY: number,
  dx: number,
  dy: number,
): ParallaxView {
  const anchor = anchorRank(layout, view, view.scale, viewportWidth, pointerY)
  const pan = anchor === null ? view.pan : clampPan(view.pan + dx / slack(layout, anchor, view.scale, viewportWidth))
  return { pan, y: view.y + dy, scale: view.scale }
}

/** The view after zooming to `scale` about the screen point (pointerX, pointerY). */
export function zoomAt(
  layout: FlowchartLayout,
  view: ParallaxView,
  viewportWidth: number,
  pointerX: number,
  pointerY: number,
  scale: number,
): ParallaxView {
  const y = pointerY - ((pointerY - view.y) / view.scale) * scale
  const anchor = anchorRank(layout, view, scale, viewportWidth, pointerY)
  if (anchor === null) return { pan: view.pan, y, scale }
  // The anchor's point under the pointer, from the rank's left edge, stays under the pointer.
  const offset = (pointerX - rankLeft(layout, anchor, view, viewportWidth)) / view.scale
  const pan = (pointerX - offset * scale - frame(layout, scale, viewportWidth).left) / slack(layout, anchor, scale, viewportWidth)
  return { pan: clampPan(pan), y, scale }
}
