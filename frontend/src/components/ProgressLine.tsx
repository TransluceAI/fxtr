import { useEffect, useId, useRef, useState } from 'react'
import { lineShares, type Counts } from '../lib/progress'
import { cx } from '../lib/cx'
import styles from './ProgressLine.module.css'

/** How tall the line is, in pixels. */
export const LINE_HEIGHT = 3
/** The narrowest a failed or running segment is drawn, so one invocation still shows. */
const MIN_FAILED = 2
const MIN_RUNNING = 3
/** The sweep's band, as a share of the finished part. */
const SWEEP_SHARE = 0.3

/**
 * A run's progress as a 3px line: done dark, then failed red, then running light, the rest the
 * track (`--progress-*` in `styles/tokens.css`). Stopped invocations are left as track. Each time
 * more of the run has finished (done or failed) than when it last drew, a sheen sweeps the done part
 * once (`useSweep`), so a run that is moving shows it even when its percentage does not change. The
 * caller places it, usually along a bottom edge.
 */
export function ProgressLine({ counts, className }: { counts: Counts; className?: string }) {
  const shares = lineShares(counts)
  const sweep = useSweep(counts.done + counts.failed)
  return (
    <div className={cx(styles.line, className)} aria-hidden>
      <span className={styles.done} style={{ width: percent(shares.done) }}>
        {sweep > 0 && <span key={sweep} className={styles.sweep} />}
      </span>
      {shares.failed > 0 && <span className={styles.failed} style={{ width: `max(${MIN_FAILED}px, ${percent(shares.failed)})` }} />}
      {shares.running > 0 && <span className={styles.running} style={{ width: `max(${MIN_RUNNING}px, ${percent(shares.running)})` }} />}
    </div>
  )
}

/**
 * The same line in an SVG, `width` wide with its top at `y`, for the flowchart's nodes; `clipPath`
 * names the shape to keep it inside (the node's rounded body).
 */
export function ProgressLineSvg({ counts, width, y, clipPath }: { counts: Counts; width: number; y: number; clipPath: string }) {
  const shares = lineShares(counts)
  const sweep = useSweep(counts.done + counts.failed)
  const ids = useId().replace(/:/g, '')
  const done = shares.done * width
  const failed = shares.failed > 0 ? Math.max(MIN_FAILED, shares.failed * width) : 0
  const running = shares.running > 0 ? Math.max(MIN_RUNNING, shares.running * width) : 0
  const band = done * SWEEP_SHARE
  return (
    <g clipPath={clipPath} aria-hidden>
      <rect y={y} width={width} height={LINE_HEIGHT} className={styles.trackSvg} />
      <rect y={y} width={done} height={LINE_HEIGHT} className={styles.doneSvg} />
      {failed > 0 && <rect x={done} y={y} width={failed} height={LINE_HEIGHT} className={styles.failedSvg} />}
      {running > 0 && <rect x={done + failed} y={y} width={running} height={LINE_HEIGHT} className={styles.runningSvg} />}
      {sweep > 0 && band > 0 && (
        <>
          <defs>
            <clipPath id={`${ids}-done`}>
              <rect y={y} width={done} height={LINE_HEIGHT} />
            </clipPath>
            <linearGradient id={`${ids}-sheen`}>
              <stop offset="0" style={{ stopColor: 'var(--progress-sheen)', stopOpacity: 0 }} />
              <stop offset="0.5" style={{ stopColor: 'var(--progress-sheen)' }} />
              <stop offset="1" style={{ stopColor: 'var(--progress-sheen)', stopOpacity: 0 }} />
            </linearGradient>
          </defs>
          <g clipPath={`url(#${ids}-done)`}>
            <rect
              key={sweep}
              x={-band}
              y={y}
              width={band}
              height={LINE_HEIGHT}
              fill={`url(#${ids}-sheen)`}
              className={styles.sweepSvg}
              style={{ '--travel': `${done + band}px` } as React.CSSProperties}
            />
          </g>
        </>
      )}
    </g>
  )
}

/** How many times `finished` has grown since the caller mounted; each new value replays a sweep. */
function useSweep(finished: number): number {
  const [sweeps, setSweeps] = useState(0)
  const last = useRef(finished)
  useEffect(() => {
    if (finished > last.current) setSweeps((n) => n + 1)
    last.current = finished
  }, [finished])
  return sweeps
}

const percent = (share: number) => `${(share * 100).toFixed(3)}%`
