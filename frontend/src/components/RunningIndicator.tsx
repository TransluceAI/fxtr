import { ArrowDown, ArrowUp } from 'lucide-react'
import { useStoreSelector } from '../lib/store'
import type { StepDoc } from '../types'
import type { CardGeometry, Viewport, ViewportStore } from './Minimap'
import { ProgressLine } from './ProgressLine'
import { cx } from '../lib/cx'
import styles from './RunningIndicator.module.css'

/** A running step off screen, and which side of the viewport it is on. */
export interface OffscreenRunning {
  id: string
  above: boolean
}

/**
 * The running step nearest the viewport, when no running step's card overlaps it; null when one
 * does, when nothing is running, or before the viewport has been measured.
 */
export function offscreenRunning(
  level: readonly StepDoc[],
  geometry: ReadonlyMap<string, CardGeometry>,
  viewport: Viewport,
): OffscreenRunning | null {
  if (viewport.height <= 0) return null
  const viewportBottom = viewport.top + viewport.height
  let nearest: OffscreenRunning | null = null
  let nearestDistance = Infinity
  for (const step of level) {
    if (step.progress.glyph.shape !== 'running') continue
    const box = geometry.get(step.id)
    if (!box) continue
    if (box.bottom > viewport.top && box.top < viewportBottom) return null
    const above = box.bottom <= viewport.top
    const distance = above ? viewport.top - box.bottom : box.top - viewportBottom
    if (distance < nearestDistance) {
      nearest = { id: step.id, above }
      nearestDistance = distance
    }
  }
  return nearest
}

/**
 * A pill floating over the trace naming the nearest running step while none is on screen: at the
 * top middle when the step is above the viewport, the bottom middle when below, with an arrow
 * pointing to it, over the running hatch (`.hatch`), and the step's progress line along its foot.
 * Clicking it scrolls to the step. It subscribes to the trace's viewport itself, so scrolling
 * re-renders it only when the step it names or its side changes. The caller places it in a
 * zero-height bar stuck to the top of the trace's scroller.
 */
export function RunningIndicator({ level, geometry, viewport, onJump }: {
  level: readonly StepDoc[]
  geometry: ReadonlyMap<string, CardGeometry>
  viewport: ViewportStore
  onJump: (id: string) => void
}) {
  // A string, since the selection is compared by identity: the side, then the step's id.
  const target = useStoreSelector(viewport, (current) => {
    const running = offscreenRunning(level, geometry, current)
    return running && `${running.above ? 'above' : 'below'}:${running.id}`
  })
  const height = useStoreSelector(viewport, (current) => current.height)
  if (target === null) return null
  const split = target.indexOf(':')
  const above = target.slice(0, split) === 'above'
  const step = level.find((candidate) => candidate.id === target.slice(split + 1))
  if (!step) throw new Error(`running step ${target.slice(split + 1)} is not in the level`)
  const counts = step.progress.counts
  return (
    <button
      // Keyed by the step, so naming another one fades in afresh and its line sweeps only for it.
      key={step.id}
      type="button"
      className={cx(styles.indicator, above ? styles.isAbove : styles.isBelow)}
      style={above ? undefined : { top: height }}
      title={`scroll to ${step.name}`}
      onClick={() => onJump(step.id)}
    >
      <span className={cx('hatch', styles.backdrop)} aria-hidden />
      {above
        ? <ArrowUp size={14} strokeWidth={2} className={styles.arrow} aria-hidden />
        : <ArrowDown size={14} strokeWidth={2} className={styles.arrow} aria-hidden />}
      <span className={styles.name}>{step.name}</span>
      {counts !== null && <ProgressLine counts={counts} className={styles.line} />}
    </button>
  )
}
