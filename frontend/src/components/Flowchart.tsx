import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { dimKind, type DimKind } from '../lib/invocations'
import { KIND_ICONS, KIND_ICON_SIZE, KIND_ICON_STROKE } from '../lib/kindIcons'
import type { StepDoc } from '../types'
import { cx } from '../lib/cx'
import { hasPrimaryModifier } from '../lib/keyboard'
import { LINE_HEIGHT, ProgressLineSvg } from './ProgressLine'
import { DIM_PITCH, DIM_TOP, edgePath, layoutLevel, measureDim, NAME_X, NODE_PAD, type LayoutNode } from './flowchartLayout'
import { panBy, placeX, rankPlacements, zoomAt, type ParallaxView } from './flowchartParallax'
import styles from './Flowchart.module.css'

const DIM_CLASS: Record<DimKind, string> = {
  mapped: styles.dimMapped,
  replica: styles.dimReplica,
  other: styles.dimOther,
}

interface Props {
  /** Identifies the level: the graph is fitted into view when this changes, and only then. */
  levelKey: string
  level: StepDoc[]
  selectedId: string | null
  onSelect: (id: string) => void
}

/**
 * How far past the viewport's side an edge to a card off screen is cut short, in screen pixels.
 * Every edge running off one side then ends at the same point, and they read as one edge going
 * that way instead of a sheaf of nearly flat curves.
 */
const EDGE_OVERHANG = 40

/**
 * A pannable, zoomable DAG for the cards in one workflow invocation. Each node shows its step's
 * state glyph (nothing when done) and, for a run, how many of its invocations are done. Panning
 * sideways is parallax (see flowchartParallax): each rank slides on its own, so the ranks that fit
 * stay on screen while a long one scrolls, and each side of a rank counts its cards off screen.
 * While a job runs the level is republished on every
 * change: the graph is laid out again only when its shape changes (a card or an edge appears), and
 * fitted into view only when a different level is shown, so a live update never moves the view.
 */
export function Flowchart({ levelKey, level, selectedId, onSelect }: Props) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ pointerId: number; x: number; y: number } | null>(null)
  const shape = level.map((step) => `${step.id}:${step.name}:${step.dims.join(',')}:${step.deps.join(',')}`).join('|')
  // The layout depends only on the graph's shape, which `shape` spells out.
  const layout = useMemo(() => layoutLevel(level), [shape])
  const current = useMemo(() => new Map(level.map((step) => [step.id, step])), [level])
  const fittedFor = useRef<string | null>(null)
  const [view, setView] = useState<ParallaxView>({ pan: 0.5, y: 0, scale: 1 })
  const [viewportWidth, setViewportWidth] = useState(0)
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const viewRef = useRef(view)
  const layoutRef = useRef(layout)
  const onSelectRef = useRef(onSelect)

  useLayoutEffect(() => {
    viewRef.current = view
  }, [view])
  useLayoutEffect(() => {
    layoutRef.current = layout
  }, [layout])
  useLayoutEffect(() => {
    onSelectRef.current = onSelect
  }, [onSelect])
  // Stable, so a memoized card re-renders only when its own props change, not on every pan frame.
  const select = useCallback((id: string) => onSelectRef.current(id), [])

  const update = (next: ParallaxView) => {
    viewRef.current = next
    setView(next)
  }

  // Opening a level starts with its complete graph fitted into the graph pane; a new layout of the
  // same level keeps the view the user has.
  useLayoutEffect(() => {
    if (!viewportRef.current) return
    const viewport = viewportRef.current
    const refit = fittedFor.current !== levelKey
    fittedFor.current = levelKey
    const fit = () => {
      const width = viewport.clientWidth
      const height = viewport.clientHeight
      if (width === 0 || height === 0) return
      const scale = Math.min(1, Math.max(0.06, Math.min(width / layout.width, height / layout.height)))
      update({ pan: 0.5, y: (height - layout.height * scale) / 2, scale })
    }
    setViewportWidth(viewport.clientWidth)
    if (refit) fit()
    let previousSize = { width: viewport.clientWidth, height: viewport.clientHeight }
    const observer = new ResizeObserver(() => {
      const nextSize = { width: viewport.clientWidth, height: viewport.clientHeight }
      if (nextSize.width === 0 || nextSize.height === 0 ||
          (nextSize.width === previousSize.width && nextSize.height === previousSize.height)) return
      const current = viewRef.current
      const canvasCenterY = (previousSize.height / 2 - current.y) / current.scale
      previousSize = nextSize
      setViewportWidth(nextSize.width)
      update({ ...current, y: nextSize.height / 2 - canvasCenterY * current.scale })
    })
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [layout])

  // React delegates wheel events, and its listener can be passive in the browser. A listener on
  // the viewport itself must be explicitly non-passive so a trackpad gesture never reaches the
  // page after it has panned or zoomed the graph.
  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    // Browsers encode a trackpad pinch as Ctrl-wheel without a physical Control key press.
    // Track the key separately so Control-scroll on Mac does not become a zoom shortcut.
    let controlHeld = false
    const syncControl = (event: KeyboardEvent) => { controlHeld = event.ctrlKey }
    const releaseControl = () => { controlHeld = false }
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      event.stopPropagation()
      const current = viewRef.current
      const bounds = viewport.getBoundingClientRect()
      const pointerX = event.clientX - bounds.left
      const pointerY = event.clientY - bounds.top
      const pinch = event.ctrlKey && !controlHeld && !event.metaKey && !event.altKey && !event.shiftKey
      if (hasPrimaryModifier(event) || pinch) {
        const nextScale = Math.min(2.5, Math.max(0.22, current.scale * Math.exp(-event.deltaY * 0.01)))
        update(zoomAt(layoutRef.current, current, viewport.clientWidth, pointerX, pointerY, nextScale))
      } else {
        update(panBy(layoutRef.current, current, viewport.clientWidth, pointerY, -event.deltaX, -event.deltaY))
      }
    }
    window.addEventListener('keydown', syncControl, true)
    window.addEventListener('keyup', syncControl, true)
    window.addEventListener('blur', releaseControl)
    viewport.addEventListener('wheel', onWheel, { passive: false })
    return () => {
      window.removeEventListener('keydown', syncControl, true)
      window.removeEventListener('keyup', syncControl, true)
      window.removeEventListener('blur', releaseControl)
      viewport.removeEventListener('wheel', onWheel)
    }
  }, [])

  const placements = useMemo(() => rankPlacements(layout, view, viewportWidth), [layout, view, viewportWidth])
  const edgePaths = useMemo(() => {
    const minX = -EDGE_OVERHANG / view.scale
    const maxX = (viewportWidth + EDGE_OVERHANG) / view.scale
    return layout.edges.map(({ points, pointRanks }) =>
      edgePath(
        points.map((point, index) => ({
          x: Math.min(maxX, Math.max(minX, placeX(placements[pointRanks[index]], point.x))),
          y: point.y,
        })),
        pointRanks,
      ),
    )
  }, [layout, placements, view.scale, viewportWidth])
  const offscreen = useMemo(() => {
    const counts = layout.ranks.map((_, rank) => ({ rank, left: 0, right: 0 }))
    for (const node of layout.nodes) {
      const x = view.scale * (node.x + placements[node.rank].shift)
      if (x + view.scale * node.width < 0) counts[node.rank].left += 1
      else if (x > viewportWidth) counts[node.rank].right += 1
    }
    return counts.filter(({ left, right }) => left > 0 || right > 0)
  }, [layout, placements, view.scale, viewportWidth])

  return (
    <section className={styles.flowchart} aria-label="Workflow flowchart">
      <div
        ref={viewportRef}
        className={styles.viewport}
        onPointerDown={(event) => {
          if (event.button !== 0 || (event.target as Element).closest('[data-flow-node]')) return
          dragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY }
          event.currentTarget.setPointerCapture(event.pointerId)
          event.currentTarget.classList.add(styles.isDragging)
        }}
        onPointerMove={(event) => {
          const drag = dragRef.current
          if (!drag || drag.pointerId !== event.pointerId) return
          const dx = event.clientX - drag.x
          const dy = event.clientY - drag.y
          dragRef.current = { ...drag, x: event.clientX, y: event.clientY }
          const viewport = event.currentTarget
          const pointerY = event.clientY - viewport.getBoundingClientRect().top
          update(panBy(layout, viewRef.current, viewport.clientWidth, pointerY, dx, dy))
        }}
        onPointerUp={(event) => {
          if (dragRef.current?.pointerId !== event.pointerId) return
          dragRef.current = null
          event.currentTarget.classList.remove(styles.isDragging)
          event.currentTarget.releasePointerCapture(event.pointerId)
        }}
      >
        <svg className={styles.graph} width="100%" height="100%">
          <defs>
            <marker
              id="flowchart-arrow"
              viewBox="0 0 6 6"
              refX="5"
              refY="3"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 6 3 L 0 6 z" className={styles.arrow} />
            </marker>
          </defs>
          <g transform={`translate(0 ${view.y}) scale(${view.scale})`}>
            {layout.edges.map(({ sourceId, targetId, secondary }, index) => (
              <path
                key={`${sourceId}->${targetId}`}
                d={edgePaths[index]}
                markerEnd="url(#flowchart-arrow)"
                className={cx(
                  styles.edge,
                  secondary && styles.isSecondary,
                  hoveredId !== null &&
                    (sourceId === hoveredId || targetId === hoveredId
                      ? styles.isConnected
                      : styles.isDimmed),
                )}
              />
            ))}
            {layout.nodes.map((node) => (
              <FlowNode
                key={node.step.id}
                node={node}
                shift={placements[node.rank].shift}
                step={current.get(node.step.id) ?? node.step}
                selected={node.step.id === selectedId}
                onSelect={select}
                onHover={setHoveredId}
              />
            ))}
          </g>
          {offscreen.map(({ rank, left, right }) => {
            const band = layout.ranks[rank]
            const y = view.y + view.scale * (band.top + band.height / 2)
            return (
              <g key={rank} className={styles.offscreen}>
                {left > 0 && <OffscreenCount x={6} y={y} count={left} side="left" />}
                {right > 0 && <OffscreenCount x={viewportWidth - 6} y={y} count={right} side="right" />}
              </g>
            )
          })}
        </svg>
      </div>
    </section>
  )
}

/** A pill at the viewport's edge, level with a rank, counting that rank's cards off that side. */
function OffscreenCount({ x, y, count, side }: { x: number; y: number; count: number; side: 'left' | 'right' }) {
  const label = side === 'left' ? `‹ ${count}` : `${count} ›`
  const width = 14 + label.length * 6.5
  return (
    <g transform={`translate(${side === 'left' ? x : x - width} ${y - 10})`}>
      <title>{`${count} more ${count === 1 ? 'card' : 'cards'} this way`}</title>
      <rect width={width} height={20} rx={10} className={styles.offscreenBody} />
      <text x={width / 2} y={14} textAnchor="middle" className={styles.offscreenLabel}>{label}</text>
    </g>
  )
}

const FlowNode = memo(function FlowNode({
  node,
  shift,
  step,
  selected,
  onSelect,
  onHover,
}: {
  node: LayoutNode
  /** How far the node's rank is slid sideways from its place in the layout. */
  shift: number
  /** The step as it is now; the layout may have been computed from an earlier render of it. */
  step: StepDoc
  selected: boolean
  onSelect: (id: string) => void
  onHover: (id: string | null) => void
}) {
  const { x, y, width, height } = node
  const { counts } = step.progress
  const KindIcon = KIND_ICONS[step.kind]
  // A run's progress line along the foot, as on its card, until everything in it is done; a card
  // at rest shows no state at all, and there is no percentage.
  const showsLine = counts !== null && counts.total > 0 && counts.done < counts.total
  const clipId = useId().replace(/:/g, '')
  const onClick = () => onSelect(step.id)
  return (
    <g
      data-flow-node
      className={cx(styles.node, selected && styles.isSelected)}
      transform={`translate(${x + shift} ${y})`}
      onClick={onClick}
      onMouseEnter={() => onHover(step.id)}
      onMouseLeave={() => onHover(null)}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onClick()
        }
      }}
    >
      <rect width={width} height={height} rx={9} className={styles.nodeBody} />
      <KindIcon x={NODE_PAD} y={12} width={KIND_ICON_SIZE} height={KIND_ICON_SIZE} strokeWidth={KIND_ICON_STROKE} className={styles.kindIcon}>
        <title>{step.kind}</title>
      </KindIcon>
      <text x={NAME_X} y={22} className={cx(styles.nodeTitle, styles.nodeName)}>
        {step.name}
      </text>
      {showsLine && (
        <>
          <clipPath id={clipId}>
            <rect width={width} height={height} rx={9} />
          </clipPath>
          <ProgressLineSvg counts={counts} width={width} y={height - LINE_HEIGHT} clipPath={`url(#${clipId})`} />
        </>
      )}
      {step.dims.map((dim, index) => {
        const dimWidth = measureDim(dim)
        return (
          <g key={dim} transform={`translate(${NODE_PAD} ${DIM_TOP + index * DIM_PITCH})`}>
            <rect width={dimWidth} height={20} rx={5} className={cx(styles.dim, DIM_CLASS[dimKind(step, dim)])} />
            <text x={dimWidth / 2} y={13} textAnchor="middle" className={styles.dimName}>{dim}</text>
          </g>
        )
      })}
    </g>
  )
})
