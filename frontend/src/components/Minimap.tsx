import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { Waypoints } from 'lucide-react'
import { dimKind, type DimKind } from '../lib/invocations'
import { NOTHING_LIT, type SpotlightStore } from '../lib/cardSpotlight'
import { Store, useStoreSelector } from '../lib/store'
import type { LevelRef, StepDoc } from '../types'
import { GlyphShape } from './StateGlyph'
import { cx } from '../lib/cx'
import styles from './Minimap.module.css'

const DIM_CLASS: Record<DimKind, string> = {
  mapped: styles.dimMapped,
  replica: styles.dimReplica,
  other: '',
}

const DOT_X = 18
const PAD_Y = 8
const ROW_H = 24
/** Where a row's name and dim pips sit below its top; the dot is centred between them. */
const NAME_DY = 8
const DIM_DY = 16
const DIM_W = 8
const DIM_GAP = 3
const NAME_X = 29
const DOT_R = 3.5

/** Where a step card sits on the large canvas, relative to the top of the level. */
export interface CardGeometry {
  top: number
  bottom: number
  center: number
}

/** The part of the large canvas currently scrolled into view. */
export interface Viewport {
  top: number
  height: number
}

/**
 * The trace's viewport as it scrolls, held outside React state: the scroller writes it on every
 * scroll, and only the minimap's viewport rectangle subscribes, so scrolling re-renders that
 * rectangle and not the job view with every card in it.
 */
export type ViewportStore = Store<Viewport>

export function createViewportStore(): ViewportStore {
  return new Store<Viewport>({ top: 0, height: 0 }, (a, b) => a.top === b.top && a.height === b.height)
}

interface Props {
  /** The root invocation's name, the first breadcrumb. */
  rootLabel: string
  /** Every invocation opened below the root, outermost first, including any below the current level. */
  trail: LevelRef[]
  /** The breadcrumb shown: 0 is the root, `k` is `trail[k - 1]`. */
  depth: number
  level: StepDoc[]
  /** The step a hovered chip names (`CardSpotlight`), or null to light no row. */
  spotlight: SpotlightStore | null
  /** The step whose incoming dependencies are drawn: the row under the pointer, else the selection. */
  focus: StepDoc | null
  /** Ids of `focus`'s dependencies that are in this level. */
  incoming: string[]
  /** Whether `focus` is a row under the pointer (rather than the selection). */
  hovering: boolean
  geometry: ReadonlyMap<string, CardGeometry>
  viewport: ViewportStore
  /** Show the level at that breadcrumb, keeping the breadcrumbs below it. */
  onShowLevel: (depth: number) => void
  /** Show the level at that breadcrumb and drop the breadcrumbs below it. */
  onClearBelow: (depth: number) => void
  onHover: (id: string | null) => void
  onJump: (id: string) => void
  onOpenFlowchart: () => void
  /** Shown right below the rows: the level's other views (`LevelViews`). */
  children?: ReactNode
}

/**
 * The breadcrumbs, then under a `trace` heading one equal-height row per step in the level with
 * the dependency graph of the focused step, then whatever follows the rows. The rows take their
 * own height (scrolling when the rail is too short), so what follows sits right under them.
 *
 * Each row leads with its step's state glyph, so the rows say where a job in progress is busy: a
 * running step as a solid dot, not a ring, on the running hatch; this is the one place done steps
 * show a dot. A step's name and dim pips are faint until it starts, and its name is red once any
 * of its invocations has failed. The focused step
 * and the steps it reads are drawn with their edges, and while a row is hovered every other glyph
 * fades. Hovering a chip lights the row of the step it names.
 */
export function Minimap({
  rootLabel,
  trail,
  depth,
  level,
  spotlight,
  focus,
  incoming,
  hovering,
  geometry,
  viewport,
  onShowLevel,
  onClearBelow,
  onHover,
  onJump,
  onOpenFlowchart,
  children,
}: Props) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  useLayoutEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const measure = () => setSize({ width: canvas.clientWidth, height: canvas.clientHeight })
    const observer = new ResizeObserver(measure)
    observer.observe(canvas)
    measure()
    return () => observer.disconnect()
  }, [])

  const contentHeight = PAD_Y * 2 + level.length * ROW_H
  const active = new Set([focus?.id, ...incoming])
  const rowCenter = (index: number) => PAD_Y + index * ROW_H + ROW_H / 2
  const focusCenter = focus ? geometry.get(focus.id)?.center ?? 0 : 0
  const lanes = [...incoming].sort(
    (a, b) =>
      Math.abs((geometry.get(a)?.center ?? 0) - focusCenter) -
      Math.abs((geometry.get(b)?.center ?? 0) - focusCenter),
  )

  return (
    <aside className={styles.minimap} aria-label="Workflow overview">
      <WorkflowBreadcrumbs
        rootLabel={rootLabel}
        trail={trail}
        depth={depth}
        onShowLevel={onShowLevel}
        onClearBelow={onClearBelow}
      />
      <div className={styles.headingRow}>
        <h2 className={styles.heading}>trace</h2>
        <FlowchartToggle active={false} onClick={onOpenFlowchart} />
      </div>
      <div className={styles.canvas} ref={canvasRef}>
        <svg width={size.width} height={contentHeight}>
          {level.map((step, index) => {
            const rowY = PAD_Y + index * ROW_H
            return (
              <g key={`card-${step.id}`}>
                <RowRect stepId={step.id} y={rowY} width={size.width} spotlight={spotlight}
                  running={step.progress.glyph.shape === 'running'} />
                <StepLabel step={step} y={rowY} />
              </g>
            )
          })}
          {focus &&
            lanes.map((depId, lane) => {
              const sourceRow = level.findIndex((step) => step.id === depId)
              const targetRow = level.findIndex((step) => step.id === focus.id)
              if (sourceRow < 0 || targetRow < 0) return null
              return (
                <path
                  key={`${depId}->${focus.id}`}
                  d={incomingPath(rowCenter(sourceRow), rowCenter(targetRow), lane)}
                  className={styles.dependency}
                />
              )
            })}
          {level.map((step, index) => (
            <GlyphShape
              key={`dot-${step.id}`}
              cx={DOT_X}
              cy={rowCenter(index)}
              r={DOT_R}
              glyph={step.progress.glyph}
              counts={step.progress.counts}
              runningDot
              muted={hovering && !active.has(step.id)}
            />
          ))}
          {geometry.size > 0 && <ViewportRect store={viewport} level={level} geometry={geometry} width={size.width} />}
          {level.map((step, index) => (
            <rect
              key={`hit-${step.id}`}
              x={0}
              y={PAD_Y + index * ROW_H}
              width={size.width}
              height={ROW_H}
              className={styles.hit}
              onMouseEnter={() => onHover(step.id)}
              onMouseLeave={() => onHover(null)}
              onClick={() => onJump(step.id)}
            />
          ))}
        </svg>
      </div>
      {children}
    </aside>
  )
}

/** A row's step name and dim pips, faint before any of its invocations has started. */
function StepLabel({ step, y }: { step: StepDoc; y: number }) {
  const { counts } = step.progress
  const started = counts === null || counts.queued < counts.total
  return (
    <g className={cx(!started && styles.notStarted)}>
      <Label step={step} y={y} />
    </g>
  )
}

function Label({ step, y }: { step: StepDoc; y: number }) {
  return (
    <>
      <text x={NAME_X} y={y + NAME_DY} className={cx(styles.name, step.progress.glyph.failed && styles.isFailed)}>{step.name}</text>
      {step.dims.map((dim, index) => (
        <line
          key={dim}
          x1={NAME_X + index * (DIM_W + DIM_GAP)}
          x2={NAME_X + index * (DIM_W + DIM_GAP) + DIM_W}
          y1={y + DIM_DY}
          y2={y + DIM_DY}
          className={cx(styles.dim, DIM_CLASS[dimKind(step, dim)])}
        />
      ))}
    </>
  )
}

export function WorkflowBreadcrumbs({
  rootLabel,
  trail,
  depth,
  onShowLevel,
  onClearBelow,
}: Pick<Props, 'rootLabel' | 'trail' | 'depth' | 'onShowLevel' | 'onClearBelow'>) {
  return (
    <nav className={styles.breadcrumbs} aria-label="Workflow path">
      {[{ pathname: rootLabel, label: rootLabel }, ...trail].map((ref, index) => (
        <button
          key={ref.pathname}
          className={cx(index === depth && styles.isActive)}
          aria-current={index === depth ? 'location' : undefined}
          onClick={() => onShowLevel(index)}
          onDoubleClick={() => onClearBelow(index)}
          title={index < trail.length ? `${ref.pathname}\ndouble-click to close the levels below` : ref.pathname}
        >
          <span>{ref.label}</span>
        </button>
      ))}
    </nav>
  )
}

export function FlowchartToggle({ active, onClick }: { active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className={cx(styles.flowButton, active && styles.isActive)}
      aria-label="Toggle workflow flowchart"
      aria-pressed={active}
      title={active ? 'Show trace' : 'Show flowchart'}
      onClick={onClick}
    >
      <Waypoints size={14} strokeWidth={1.8} />
    </button>
  )
}

/** A dependency edge in the gutter left of the dots; each lane sits 2px further out. */
function incomingPath(sourceY: number, targetY: number, lane: number): string {
  const corner = Math.min(4, Math.abs(targetY - sourceY) / 2)
  const gutterX = DOT_X - 8 - lane * 2
  const direction = targetY >= sourceY ? 1 : -1
  return [
    `M ${DOT_X} ${sourceY}`,
    `H ${gutterX + corner}`,
    `Q ${gutterX} ${sourceY} ${gutterX} ${sourceY + direction * corner}`,
    `V ${targetY - direction * corner}`,
    `Q ${gutterX} ${targetY} ${gutterX + corner} ${targetY}`,
    `H ${DOT_X}`,
  ].join(' ')
}

/** A row's background, lit while a hovered chip names its step, and otherwise hatched while it
 *  runs (the shared `.hatch`, as HTML inside the SVG); it alone re-renders as the spotlight moves. */
function RowRect({ stepId, y, width, spotlight, running }: {
  stepId: string
  y: number
  width: number
  spotlight: SpotlightStore | null
  running: boolean
}) {
  const lit = useStoreSelector(spotlight ?? NOTHING_LIT, (current) => current?.id === stepId)
  const rowWidth = Math.max(0, width - 6)
  return (
    <>
      <rect x={3} y={y} width={rowWidth} height={ROW_H} className={cx(styles.card, lit && styles.isSpotlit)} />
      {running && !lit && (
        <foreignObject x={3} y={y} width={rowWidth} height={ROW_H} className={styles.hatch}>
          <div className="hatch" />
        </foreignObject>
      )}
    </>
  )
}

/** The part of the trace scrolled into view, as a rectangle over the rows it spans. */
function ViewportRect({ store, level, geometry, width }: {
  store: ViewportStore
  level: StepDoc[]
  geometry: ReadonlyMap<string, CardGeometry>
  width: number
}) {
  const viewport = useStoreSelector(store, (current) => current)
  const top = contentToMinimapY(viewport.top, level, geometry)
  const bottom = contentToMinimapY(viewport.top + viewport.height, level, geometry)
  return (
    <rect x={3} y={top} width={Math.max(0, width - 6)} height={Math.max(0, bottom - top)} rx={4}
      className={styles.viewport} />
  )
}

/** Map a large-canvas y through unequal card heights into the minimap's equal-height rows. */
function contentToMinimapY(
  contentY: number,
  level: StepDoc[],
  geometry: ReadonlyMap<string, CardGeometry>,
): number {
  if (level.length === 0) return PAD_Y
  const rows = level
    .map((step) => geometry.get(step.id))
    .filter((row): row is CardGeometry => row !== undefined)
  if (rows.length !== level.length) return PAD_Y

  const miniTop = PAD_Y
  const miniBottom = PAD_Y + level.length * ROW_H
  if (contentY <= rows[0].top) return miniTop

  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index]
    const rowTop = miniTop + index * ROW_H
    const rowBottom = rowTop + ROW_H
    if (contentY <= row.bottom) return interpolate(contentY, row.top, row.bottom, rowTop, rowBottom)
    // Between two cards the minimap has no gap, so the gap collapses onto the row boundary.
    const next = rows[index + 1]
    if (next && contentY < next.top) return rowBottom
  }

  return miniBottom
}

function interpolate(value: number, from: number, to: number, outFrom: number, outTo: number) {
  if (to <= from) return outTo
  const progress = Math.max(0, Math.min(1, (value - from) / (to - from)))
  return outFrom + (outTo - outFrom) * progress
}
