/**
 * The detail view's columns on screen, left to right from the detail view's left edge, each as
 * wide as the layout says (`DEFAULT_COLUMN_WIDTH` until resized). Dragging the boundary at a
 * column's right edge resizes that column, and the columns right of it keep their widths and move.
 * Columns narrower together than the detail view leave the rest of it empty; wider, they run on
 * past its right edge (and `TRAILING_GUTTER` beyond, so the last one's edge can be grabbed), and
 * scrolling sideways over them (a trackpad, Shift and the wheel, or the bar at their foot) slides
 * them left, over the trace, until the last column is in view. Opening a panel never slides them
 * further over the trace (only back, to show its column's left edge). When the columns shrink (panels
 * closed or replaced), the slide is kept, so nothing on screen moves and the room they left at the
 * right stays empty until scrolled back left. A resize held at the screen's edge
 * slides them along as the column grows (`ResizeHandle`). Each column stacks its panels top to
 * bottom, sized as `panelSizing.ts` says (`useFitHeights`).
 *
 * Where a hovered or dragged link would open is drawn over everything (`Highlight`): a horizontal
 * line where it would go into a column, a vertical line where a new column would go, an outline
 * round (what shows of) a panel it would use or replace. Links and panel heads drop anywhere over
 * the columns: near a column's side edge for a new column there, over the top or bottom quarter of
 * what shows of a panel for above or below it, over the middle of that to replace it, below a
 * column's panels for its end. Nothing moves while dragging; once the layout changes, the panels
 * slide to their new places
 * (`useLayoutAnimation`).
 */

import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useStableCallback, useStoreSelector } from '../lib/store'
import { useModifierHeld } from '../lib/modifierClick'
import { cx } from '../lib/cx'
import { getDragPayload, hasFrameDrag } from './drag'
import { allPanels, columnLefts, type Column, type Columns, type PanelInstance, type Placement } from './columns'
import { MIN_PANEL_HEIGHT, PANEL_SIZING, allotHeights, naturalHeight } from './panelSizing'
import { PANEL_GAP, type Decision } from './placement'
import type { ColumnsState } from './useColumns'
import styles from './ColumnsView.module.css'

/** How near a column's side edge a drop makes a new column there: this share of the column's
 *  width, and at least `EDGE_DROP_MIN` px. */
const EDGE_DROP_SHARE = 0.25
const EDGE_DROP_MIN = 80

export function ColumnsView({ state, renderPanel, onSlide }: {
  state: ColumnsState
  renderPanel: (panel: PanelInstance) => ReactNode
  /** How far the columns are slid left over the trace (raised over it while any), in px. */
  onSlide: (slide: number) => void
}) {
  const { columns } = state
  // Alt over any panel's close button lights them all (Alt-click closes every panel), in CSS.
  useModifierHeld(true)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const stripRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [slide, setSlide] = useState(0)
  const count = columns.length
  // Each panel renders once, into a node of its own that its slot in the layout holds, so moving
  // it to another column or place keeps its state (a table's sort, what is expanded).
  const nodes = useRef(new Map<string, HTMLDivElement>())
  const nodeFor = useCallback((panelId: string): HTMLDivElement => {
    let node = nodes.current.get(panelId)
    if (!node) {
      node = document.createElement('div')
      node.className = styles.cardBody!
      nodes.current.set(panelId, node)
    }
    return node
  }, [])
  useEffect(() => {
    const open = new Set(allPanels(columns).map((panel) => panel.id))
    for (const id of nodes.current.keys()) if (!open.has(id)) nodes.current.delete(id)
  }, [columns])
  const frameRef = useRef<HTMLDivElement>(null)
  useLayoutAnimation(frameRef, columns)
  const lefts = columnLefts(columns)
  const total = count === 0 ? 0 : lefts[count - 1]! + columns[count - 1]!.width
  // Room after the last column, so its right edge is never flush with the detail view's.
  const overflow = Math.max(0, total + (count > 0 ? TRAILING_GUTTER : 0) - size.width)
  // The slide is not cut back when the columns shrink (panels closed or replaced): they stay where
  // they are on screen, with room left empty at the right, until scrolled back left. The range
  // scrolled over is the overflow, or the slide while it is past it.
  const shownSlide = count === 0 ? 0 : slide
  const range = Math.max(overflow, shownSlide)
  // For a resize auto-scrolling at the screen's edge, which reads and moves the slide per frame.
  const slideRef = useRef(shownSlide)
  slideRef.current = shownSlide
  const slideTo = useCallback((next: number) => setSlide(Math.max(0, next)), [])
  const edge = useCallback(() => sentinelRef.current?.getBoundingClientRect().right ?? window.innerWidth, [])
  // A slide re-renders the window alone: the strip is memoized, and so are the panels' portals, so
  // no panel renders again and nothing is measured while the columns slide.
  const portals = useMemo(() => allPanels(columns).map((panel) => createPortal(renderPanel(panel), nodeFor(panel.id), panel.id)),
    [columns, renderPanel, nodeFor])

  useLayoutEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel) return
    const measure = () => setSize({ width: sentinel.clientWidth, height: sentinel.clientHeight })
    const observer = new ResizeObserver(measure)
    observer.observe(sentinel)
    measure()
    return () => observer.disconnect()
  }, [])

  useLayoutEffect(() => onSlide(shownSlide), [shownSlide, onSlide])
  useEffect(() => {
    if (count === 0) setSlide(0)
  }, [count])

  // The geometry the placement rules read: as tall as the detail view, each panel as it measures.
  useLayoutEffect(() => {
    state.geometry.current = () => ({
      columnHeight: sentinelRef.current?.clientHeight ?? window.innerHeight,
      panelHeight: (panelId) => {
        const card = panelElement(stripRef.current, panelId)
        return card ? naturalHeight(card) : 0
      },
    })
    return () => {
      state.geometry.current = null
    }
  }, [state.geometry])

  // Sideways wheel scrolling slides the columns; a listener of its own, since React's is passive.
  // A trackpad sends several wheel events a frame: their deltas add up into one slide a frame.
  useEffect(() => {
    const frame = frameRef.current
    if (!frame) return
    let pending = 0
    let request = 0
    const onWheel = (event: WheelEvent) => {
      if (range <= 0) return
      const sideways = Math.abs(event.deltaX) > Math.abs(event.deltaY)
      if (!sideways && !event.shiftKey) return
      event.preventDefault()
      pending += sideways ? event.deltaX : event.deltaY
      if (request !== 0) return
      request = requestAnimationFrame(() => {
        const delta = pending
        pending = 0
        request = 0
        setSlide((current) => clamp(current + delta, 0, Math.max(overflow, current)))
      })
    }
    frame.addEventListener('wheel', onWheel, { passive: false })
    return () => {
      frame.removeEventListener('wheel', onWheel)
      cancelAnimationFrame(request)
    }
  }, [overflow, range])

  // A drag ended anywhere (dropped elsewhere, or cancelled) lights nothing any more.
  useEffect(() => {
    const clear = () => state.highlight.set(null)
    window.addEventListener('dragend', clear)
    return () => window.removeEventListener('dragend', clear)
  }, [state.highlight])

  // A panel a link opened or used is scrolled into view, as little as will show its head and the
  // start of its body (`revealScroll`), and flashes. The columns are never slid further over the
  // trace for it: only back, as little as will show its column's left edge when that is slid out of
  // view at the left (a column slid over the trace is still in view). A panel opened past the
  // detail view's right edge is reached by sliding the columns by hand.
  const reveal = useStoreSelector(state.reveal, (value) => value)
  useLayoutEffect(() => {
    if (!reveal) return
    const element = panelElement(stripRef.current, reveal.panelId)
    if (!element) return
    const column = element.parentElement!
    const hidden = clipLeft(frameRef.current!) - column.getBoundingClientRect().left
    if (hidden > 0) setSlide((current) => Math.max(0, current - hidden))
    const scroll = revealScroll(column, element)
    if (scroll !== 0) column.scrollBy({ top: scroll, behavior: 'smooth' })
    flashPanel(stripRef.current, reveal.panelId)
    // Only a new reveal scrolls: a resize later leaves the columns where they are.
  }, [reveal])

  // A panel flashes whenever what it shows changes (its address, or a param its address uses),
  // whatever changed it: a link it followed, a change it followed (of the panel before it, or of the
  // slice), its history, its view, a pick in it.
  const shownTexts = useRef(new Map<string, string>())
  useLayoutEffect(() => {
    const next = new Map<string, string>()
    for (const panel of allPanels(columns)) {
      const text = state.stateOf(panel.id).resolution.text
      const before = shownTexts.current.get(panel.id)
      if (before !== undefined && before !== text) flashPanel(stripRef.current, panel.id)
      next.set(panel.id, text)
    }
    shownTexts.current = next
  }, [columns, state.stateOf])

  const dropPlacement = (event: React.DragEvent): Placement => {
    const strip = stripRef.current
    if (!strip || count === 0) return { kind: 'newColumn', index: 0 }
    const x = event.clientX - strip.getBoundingClientRect().left
    if (x >= total) return { kind: 'newColumn', index: count }
    const column = Math.max(0, lefts.filter((left) => left <= x).length - 1)
    const width = columns[column]!.width
    const within = x - lefts[column]!
    const edge = Math.max(EDGE_DROP_MIN, width * EDGE_DROP_SHARE)
    if (within < edge) return { kind: 'newColumn', index: column }
    if (within > width - edge) return { kind: 'newColumn', index: column + 1 }
    const stack = columns[column]!.panels
    // A panel's zones are quarters of the part of it on screen, so a panel taller than the column
    // is replaced by a drop on the middle of what shows of it.
    const view = strip.querySelector<HTMLElement>(`[data-column="${column}"]`)!.getBoundingClientRect()
    for (let index = 0; index < stack.length; index++) {
      const box = panelElement(strip, stack[index]!.id)?.getBoundingClientRect()
      if (!box) continue
      const top = Math.max(box.top, view.top)
      const bottom = Math.min(box.bottom, view.bottom)
      if (bottom <= top) continue
      if (event.clientY < top) return { kind: 'into', column, index }
      if (event.clientY <= bottom) {
        const at = (event.clientY - top) / (bottom - top)
        if (at < 0.25) return { kind: 'into', column, index }
        if (at > 0.75) return { kind: 'into', column, index: index + 1 }
        return { kind: 'replace', panelId: stack[index]!.id }
      }
    }
    return { kind: 'into', column, index: stack.length }
  }

  return (
    <div className={cx(styles.root, PANEL_SIZING === 'fit' ? styles.fit : styles.grow)}>
      <div className={styles.sentinel} ref={sentinelRef} />
      <div
        ref={frameRef}
        className={cx(styles.frame, shownSlide > 0 && styles.isSliding)}
        style={{ left: -shownSlide }}
        onDragOver={(event) => {
          if (!hasFrameDrag(event.dataTransfer)) return
          event.preventDefault()
          event.dataTransfer.dropEffect = event.dataTransfer.effectAllowed === 'move' ? 'move' : 'copy'
          state.highlight.set(dropPlacement(event))
        }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) state.highlight.set(null)
        }}
        onDrop={(event) => {
          const payload = getDragPayload(event.dataTransfer)
          state.highlight.set(null)
          if (!payload) return
          event.preventDefault()
          const placement = dropPlacement(event)
          if (payload.kind === 'move') state.move(payload.panelId, placement)
          else state.follow(payload.target, payload.originId, { placement })
        }}
      >
        <Strip columns={columns} stripRef={stripRef} nodeFor={nodeFor}
          resizeColumn={state.resizeColumn} edge={edge} slide={slideRef} onSlide={slideTo} />
        {portals}
        {count === 0 && (
          <div className={styles.empty}>Click a link or a card's open button to show it here, or drag one here.</div>
        )}
      </div>
      {range > 0 && (
        <Scrollbar width={size.width} content={size.width + range} offset={shownSlide}
          onOffset={(next) => setSlide(clamp(next, 0, range))} />
      )}
      <Highlight state={state} strip={stripRef} sentinel={sentinelRef} slide={shownSlide} />
    </div>
  )
}

/**
 * The columns side by side, each its panels' slots (which the panels' own nodes are put in,
 * `nodeFor`) with the boundaries between them, and the handles resizing the columns. Memoized, and
 * handed nothing that changes as the columns slide, so a slide neither renders it again nor runs
 * its measuring (`useFitHeights`, after every render).
 */
const Strip = memo(function Strip({ columns, stripRef, nodeFor, resizeColumn, edge, slide, onSlide }: {
  columns: Columns
  stripRef: React.RefObject<HTMLDivElement>
  nodeFor: (panelId: string) => HTMLDivElement
  resizeColumn: (columnId: string, width: number) => void
  edge: () => number
  slide: React.MutableRefObject<number>
  onSlide: (slide: number) => void
}) {
  const sizing = useFitHeights(stripRef, columns)
  const lefts = columnLefts(columns)
  const total = columns.length === 0 ? 0 : lefts[columns.length - 1]! + columns[columns.length - 1]!.width
  return (
    <div className={styles.strip} ref={stripRef} style={{ width: total }}>
      {columns.map((column, index) => (
        <div key={column.id} className={styles.column} data-column={index}
          style={{ width: column.width, overflowY: sizing.scrolls(column.id) ? 'auto' : 'hidden' }}>
          {column.panels.map((panel, at) => (
            <Fragment key={panel.id}>
              {at > 0 && (
                <RowGap
                  onResize={sizing.overflowing(column.id) && !column.panels[at - 1]!.collapsed && !panel.collapsed
                    ? (event) => sizing.startResize(event, column, at - 1)
                    : null} />
              )}
              <div className={styles.card} data-panel-id={panel.id}
                style={{ maxHeight: sizing.cap(panel.id) ?? undefined }}
                ref={(slot) => {
                  const node = slot && nodeFor(panel.id)
                  if (slot && node && node.parentElement !== slot) slot.appendChild(node)
                }} />
            </Fragment>
          ))}
        </div>
      ))}
      {columns.map((column, index) => (
        <ResizeHandle key={column.id} left={lefts[index]! + column.width} width={column.width}
          onResize={(width) => resizeColumn(column.id, width)} edge={edge} slide={slide} onSlide={onSlide} />
      ))}
    </div>
  )
})

/**
 * The panels' caps in `fit` sizing (`panelSizing.ts`), measured after every render and every
 * quarter second (content loading inside a capped panel changes nothing the layout can observe,
 * since its box stays the same), and the drags of the boundaries between panels that fix their
 * heights. In `grow` sizing, nothing is capped.
 */
function useFitHeights(stripRef: React.RefObject<HTMLDivElement | null>, columns: Columns) {
  const [caps, setCaps] = useState<ReadonlyMap<string, number>>(new Map())
  const capsRef = useRef(caps)
  capsRef.current = caps
  /** Heights fixed by a drag, by column, while the column's panels are the ones they were. */
  const fixed = useRef(new Map<string, { arrangement: string; heights: Map<string, number> }>())

  /** The columns whose panels do not fit even at their least height: they scroll, as a fallback.
   *  The rest are clipped, so the half pixels heights are rounded by never show a scrollbar. */
  const [scrolling, setScrolling] = useState<ReadonlySet<string>>(new Set())
  const scrollingRef = useRef(scrolling)
  scrollingRef.current = scrolling

  const measure = useStableCallback(() => {
    const strip = stripRef.current
    if (PANEL_SIZING !== 'fit' || !strip) return
    const next = new Map<string, number>()
    const nextScrolling = new Set<string>()
    columns.forEach((column, index) => {
      const element = strip.querySelector<HTMLElement>(`[data-column="${index}"]`)
      if (!element) return
      const style = getComputedStyle(element)
      const available = element.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)
        - PANEL_GAP * (column.panels.length - 1)
      const cards = column.panels.map((panel) => panelElement(strip, panel.id))
      if (cards.some((card) => !card)) return
      const entry = fixed.current.get(column.id)
      const kept = entry?.arrangement === arrangementOf(column) ? entry.heights : null
      const naturals = cards.map((card) => naturalHeight(card!))
      const allotted = allotHeights(
        naturals,
        column.panels.map((panel) => kept?.get(panel.id)),
        column.panels.map((panel) => !!panel.collapsed),
        available,
      )
      // Rounded down, so the panels never add up to more than the column.
      allotted.forEach((cap, at) => {
        if (cap !== null) next.set(column.panels[at]!.id, Math.floor(cap))
      })
      const total = allotted.reduce<number>((sum, cap, at) => sum + Math.min(cap ?? Infinity, naturals[at]!), 0)
      if (total > available + 1) nextScrolling.add(column.id)
    })
    const current = capsRef.current
    if (next.size !== current.size || [...next].some(([id, cap]) => current.get(id) !== cap)) setCaps(next)
    const currentScrolling = scrollingRef.current
    if (nextScrolling.size !== currentScrolling.size || [...nextScrolling].some((id) => !currentScrolling.has(id)))
      setScrolling(nextScrolling)
  })
  useLayoutEffect(() => measure())
  useEffect(() => {
    if (PANEL_SIZING !== 'fit') return
    const timer = window.setInterval(measure, 250)
    return () => window.clearInterval(timer)
  }, [measure])

  const overflowing = (columnId: string) =>
    columns.find((column) => column.id === columnId)?.panels.some((panel) => caps.has(panel.id)) ?? false

  /** Drag the boundary below the column's panel `index`. Dragging down grows the panel above it (up
   *  to its content's height), taking the height from the panels below, nearest first, each down to
   *  its least height, so a neighbour at its least is pushed down and the ones beyond it shrink;
   *  dragging up does the same the other way. Every open panel in the column keeps the height it
   *  has (collapsed ones take no part). */
  const startResize = (event: React.PointerEvent, column: Column, index: number) => {
    const strip = stripRef.current
    if (!strip) return
    event.preventDefault()
    const open = column.panels.flatMap((panel, at) => {
      if (panel.collapsed) return []
      const card = panelElement(strip, panel.id)!
      const natural = naturalHeight(card)
      return [{ id: panel.id, at, height: card.offsetHeight, natural, least: Math.min(MIN_PANEL_HEIGHT, natural) }]
    })
    const above = open.filter((panel) => panel.at <= index)
    const below = open.filter((panel) => panel.at > index)
    const start = event.clientY
    const target = event.currentTarget as HTMLElement
    target.setPointerCapture(event.pointerId)
    const onMove = (move: PointerEvent) => {
      const heights = new Map(open.map((panel) => [panel.id, panel.height]))
      const delta = move.clientY - start
      // The panel next to the boundary on the side it moves away from grows; the side it moves
      // towards gives up height, nearest first.
      const [grower, givers] = delta > 0 ? [above.at(-1), below] : [below[0], [...above].reverse()]
      if (grower) {
        const spare = givers.reduce((sum, panel) => sum + panel.height - panel.least, 0)
        let grow = Math.min(Math.abs(delta), grower.natural - grower.height, spare)
        heights.set(grower.id, grower.height + Math.max(0, grow))
        for (const panel of givers) {
          if (grow <= 0) break
          const take = Math.min(grow, panel.height - panel.least)
          heights.set(panel.id, panel.height - take)
          grow -= take
        }
      }
      fixed.current.set(column.id, { arrangement: arrangementOf(column), heights })
      measure()
    }
    const onUp = () => {
      target.removeEventListener('pointermove', onMove)
      target.removeEventListener('pointerup', onUp)
      target.removeEventListener('pointercancel', onUp)
    }
    target.addEventListener('pointermove', onMove)
    target.addEventListener('pointerup', onUp)
    target.addEventListener('pointercancel', onUp)
  }

  /** Whether a column scrolls: always in `grow` sizing; in `fit`, only when its panels do not fit
   *  even at their least height. */
  const scrolls = (columnId: string) => PANEL_SIZING === 'grow' || scrolling.has(columnId)

  return { cap: (panelId: string) => caps.get(panelId) ?? null, overflowing, scrolls, startResize }
}

function arrangementOf(column: Column): string {
  return column.panels.map((panel) => panel.id).join(',')
}

/** The space between two panels in a column; in `fit` sizing, while the column's panels are
 *  capped, a boundary dragged to trade height between them. */
function RowGap({ onResize }: { onResize: ((event: React.PointerEvent) => void) | null }) {
  return (
    <div className={cx(styles.rowGap, onResize && styles.isResizable)}
      {...(onResize ? { role: 'separator', 'aria-orientation': 'horizontal' as const, onPointerDown: onResize } : {})} />
  )
}

/** How long a panel takes to slide to its new place, or to appear, in ms. */
const LAYOUT_MS = 240
const LAYOUT_EASING = 'cubic-bezier(0.2, 0.8, 0.2, 1)'

/**
 * Animate each change of the layout (a drop, a move, a panel opened or closed): every panel that
 * was on screen before slides from where it was to where it is now, and a new one fades and
 * grows in. The old places are read from the DOM while rendering the new layout, before it is
 * committed (the render-time equivalent of `getSnapshotBeforeUpdate`), relative to the columns'
 * window, so a slide of the window itself is not animated. A column scrolled to its top lets its
 * panels show outside it while they move (one moving across columns would otherwise be clipped);
 * a scrolled one keeps clipping them.
 */
function useLayoutAnimation(frameRef: React.RefObject<HTMLDivElement | null>, columns: Columns) {
  // Only a change in where panels are animates: a column being resized moves them every frame.
  const arrangement = columns.map((column) => column.panels.map((panel) => panel.id).join(',')).join('|')
  const previous = useRef<{ arrangement: string; rects: Map<string, DOMRect> } | null>(null)
  if (previous.current === null || previous.current.arrangement !== arrangement) {
    const frame = frameRef.current
    previous.current = { arrangement, rects: frame && previous.current ? cardRects(frame) : new Map() }
  }
  useLayoutEffect(() => {
    const frame = frameRef.current
    const before = previous.current?.rects
    if (!frame || !before || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const after = cardRects(frame)
    const moved: HTMLElement[] = []
    for (const [panelId, rect] of after) {
      const card = frame.querySelector<HTMLElement>(`[data-panel-id="${CSS.escape(panelId)}"]`)!
      const old = before.get(panelId)
      if (!old) {
        card.animate([{ opacity: 0, transform: 'scale(0.97)' }, { opacity: 1, transform: 'none' }],
          { duration: LAYOUT_MS, easing: LAYOUT_EASING })
        continue
      }
      const dx = old.left - rect.left
      const dy = old.top - rect.top
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue
      moved.push(card)
      card.style.zIndex = '1'
      card.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
        { duration: LAYOUT_MS, easing: LAYOUT_EASING })
    }
    if (moved.length === 0) return
    // Overflow is set on the columns' own style by the layout (whether they scroll), so it is put back
    // as it was rather than cleared.
    const unclipped = Array.from(frame.querySelectorAll<HTMLElement>('[data-column]'))
      .filter((column) => column.scrollTop === 0)
      .map((column) => ({ column, x: column.style.overflowX, y: column.style.overflowY }))
    for (const { column } of unclipped) column.style.overflow = 'visible'
    const restore = () => {
      for (const { column, x, y } of unclipped) {
        column.style.overflowX = x
        column.style.overflowY = y
      }
      for (const card of moved) card.style.zIndex = ''
    }
    const timer = window.setTimeout(restore, LAYOUT_MS)
    return () => {
      window.clearTimeout(timer)
      restore()
    }
  }, [arrangement])
}

/** Every panel's box, relative to the columns' window. */
function cardRects(frame: HTMLElement): Map<string, DOMRect> {
  const origin = frame.getBoundingClientRect()
  const rects = new Map<string, DOMRect>()
  for (const card of frame.querySelectorAll<HTMLElement>('[data-panel-id]')) {
    const box = card.getBoundingClientRect()
    rects.set(card.dataset.panelId!, new DOMRect(box.left - origin.left, box.top - origin.top, box.width, box.height))
  }
  return rects
}

/** How much of a revealed panel's body, below its head, is scrolled into view (all of a shorter
 *  one), in px. */
const REVEAL_BODY = 80

/** How far to scroll `column` so `panel` is in view, as little as will do: nothing when its head
 *  and the start of its body are on screen already; else just enough to bring them on screen,
 *  never so far that the panel's top goes off the top. */
function revealScroll(column: HTMLElement, panel: HTMLElement): number {
  const head = panel.querySelector<HTMLElement>('.detail-head')
  const top = panel.getBoundingClientRect().top - column.getBoundingClientRect().top
  const wanted = Math.min(panel.offsetHeight, (head?.offsetHeight ?? 0) + REVEAL_BODY)
  if (top < 0) return top - PANEL_GAP
  const below = top + wanted - column.clientHeight
  if (below <= 0) return 0
  return Math.min(below + PANEL_GAP, top - PANEL_GAP)
}

/** Where on screen what `frame` shows is cut off at the left: the left edge of the nearest box
 *  around it that clips (the columns slide past the detail view's own edge, over the trace). */
function clipLeft(frame: HTMLElement): number {
  for (let box = frame.parentElement; box; box = box.parentElement) {
    if (getComputedStyle(box).overflowX !== 'visible') return box.getBoundingClientRect().left + box.clientLeft
  }
  return 0
}

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value))
}

/** Flash a panel's card, from the start even while it is flashing already. The class is the card's
 *  own to set: React never renders its className again, which stays `card`. */
function flashPanel(strip: HTMLElement | null, panelId: string): void {
  const card = panelElement(strip, panelId)
  if (!card) return
  card.classList.remove(styles.isFlash!)
  void card.offsetWidth
  card.classList.add(styles.isFlash!)
}

function panelElement(strip: HTMLElement | null, panelId: string): HTMLElement | null {
  return strip?.querySelector<HTMLElement>(`[data-panel-id="${CSS.escape(panelId)}"]`) ?? null
}

/** How much room is left after the last column, in px. */
const TRAILING_GUTTER = 48
/** How near the detail view's right edge a resize drag starts sliding the columns, in px, and how
 *  fast they slide at the edge itself, in px a frame. */
const AUTO_SCROLL_ZONE = 40
const AUTO_SCROLL_MAX = 18

/**
 * The boundary at a column's right edge (`left`, in the strip): dragging it resizes the column.
 * Held within `AUTO_SCROLL_ZONE` of the detail view's right edge (or past it), the drag slides the
 * columns left every frame, faster nearer the edge, and the column grows by as much, so the
 * boundary stays under the pointer and a column can be grown past the screen's edge.
 */
function ResizeHandle({ left, width, onResize, edge, slide, onSlide }: {
  left: number
  width: number
  onResize: (width: number) => void
  /** The detail view's right edge, in viewport px. */
  edge: () => number
  slide: React.MutableRefObject<number>
  onSlide: (slide: number) => void
}) {
  return (
    <div
      className={styles.resizeHandle}
      style={{ left }}
      role="separator"
      aria-orientation="vertical"
      onPointerDown={(event) => {
        event.preventDefault()
        const start = event.clientX
        const from = width
        const slideFrom = slide.current
        let pointer = event.clientX
        let slid = slideFrom
        const target = event.currentTarget
        target.setPointerCapture(event.pointerId)
        const apply = () => onResize(from + pointer - start + slid - slideFrom)
        let frame = 0
        const tick = () => {
          const into = pointer - (edge() - AUTO_SCROLL_ZONE)
          if (into > 0) {
            slid += AUTO_SCROLL_MAX * Math.min(1, into / AUTO_SCROLL_ZONE)
            onSlide(slid)
            apply()
          }
          frame = requestAnimationFrame(tick)
        }
        frame = requestAnimationFrame(tick)
        const onMove = (move: PointerEvent) => {
          pointer = move.clientX
          apply()
        }
        const onUp = () => {
          cancelAnimationFrame(frame)
          target.removeEventListener('pointermove', onMove)
          target.removeEventListener('pointerup', onUp)
          target.removeEventListener('pointercancel', onUp)
        }
        target.addEventListener('pointermove', onMove)
        target.addEventListener('pointerup', onUp)
        target.addEventListener('pointercancel', onUp)
      }}
    />
  )
}

/** A bar along the detail view's foot standing for the columns' sideways slide: its thumb is the
 *  part in view, and dragging it slides them. */
function Scrollbar({ width, content, offset, onOffset }: {
  width: number
  content: number
  offset: number
  onOffset: (offset: number) => void
}) {
  const scale = width / content
  return (
    <div className={styles.scrollbar}>
      <div
        className={styles.thumb}
        style={{ left: offset * scale, width: width * scale }}
        onPointerDown={(event) => {
          const start = event.clientX
          const from = offset
          const target = event.currentTarget
          target.setPointerCapture(event.pointerId)
          const onMove = (move: PointerEvent) => onOffset(from + (move.clientX - start) / scale)
          const onUp = () => {
            target.removeEventListener('pointermove', onMove)
            target.removeEventListener('pointerup', onUp)
            target.removeEventListener('pointercancel', onUp)
          }
          target.addEventListener('pointermove', onMove)
          target.addEventListener('pointerup', onUp)
          target.addEventListener('pointercancel', onUp)
        }}
      />
    </div>
  )
}

/** Where the hovered or dragged link would open, drawn in viewport coordinates over everything
 *  (so over the trace too, when the columns are empty or a new column goes at their left). */
function Highlight({ state, strip, sentinel, slide }: {
  state: ColumnsState
  strip: React.RefObject<HTMLDivElement | null>
  sentinel: React.RefObject<HTMLDivElement | null>
  slide: number
}) {
  const decision = useStoreSelector(state.highlight, (value) => value)
  if (!decision || !sentinel.current) return null
  const box = highlightBox(decision, state, strip.current, sentinel.current.getBoundingClientRect(), slide)
  if (!box) return null
  return createPortal(
    <div className={cx(styles.highlight, styles[box.kind])}
      style={{ left: box.left, top: box.top, width: box.width, height: box.height }} />,
    document.body,
  )
}

/** How thick a highlight's line is, in px. */
const LINE_WIDTH = 6

type Box = { kind: 'outline' | 'hline' | 'vline'; left: number; top: number; width: number; height: number }

function highlightBox(
  decision: Decision,
  state: ColumnsState,
  strip: HTMLElement | null,
  view: DOMRect,
  slide: number,
): Box | null {
  const columns = state.columns
  const lefts = columnLefts(columns)
  const stripLeft = view.left - slide
  switch (decision.kind) {
    case 'existing':
    case 'replace': {
      const element = panelElement(strip, decision.panelId)
      if (!element) return null
      // Outline what shows of the panel: a tall one runs past its column's top and bottom.
      const rect = element.getBoundingClientRect()
      const column = element.parentElement!.getBoundingClientRect()
      const top = Math.max(rect.top, column.top)
      const bottom = Math.min(rect.bottom, column.bottom)
      return { kind: 'outline', left: rect.left, top, width: rect.width, height: Math.max(0, bottom - top) }
    }
    case 'newColumn': {
      const last = columns.length - 1
      const offset = decision.index <= last ? lefts[decision.index]! : last >= 0 ? lefts[last]! + columns[last]!.width : 0
      const x = stripLeft + offset
      return { kind: 'vline', left: x - LINE_WIDTH / 2, top: view.top, width: LINE_WIDTH, height: view.height }
    }
    case 'into': {
      if (decision.column >= columns.length) return highlightBox({ kind: 'newColumn', index: columns.length }, state, strip, view, slide)
      const stack = columns[decision.column]!.panels
      const width = columns[decision.column]!.width
      const left = stripLeft + lefts[decision.column]! + PANEL_GAP / 2
      const above = decision.index > 0 ? panelElement(strip, stack[decision.index - 1]!.id)?.getBoundingClientRect() : null
      const y = above ? above.bottom + PANEL_GAP / 2 : view.top + PANEL_GAP / 2
      return { kind: 'hline', left, top: y - LINE_WIDTH / 2, width: width - PANEL_GAP, height: LINE_WIDTH }
    }
  }
}
