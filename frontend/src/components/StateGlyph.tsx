import { settledFraction, toneOf, type Counts, type Glyph, type RunState } from '../lib/progress'
import { arcPath } from '../lib/ring'
import styles from './StateGlyph.module.css'

const LABELS: Record<RunState, string> = {
  waiting: 'waiting on inputs',
  queued: 'queued',
  running: 'running',
  done: 'done',
  failed: 'failed',
  stopped: 'stopped',
}

/** What a glyph says, for its tooltip. */
export function glyphLabel(glyph: Glyph): string {
  return glyph.failed && glyph.shape !== 'failed' ? `${LABELS[glyph.shape]} · some failed` : LABELS[glyph.shape]
}

/** Whether a glyph is the quiet done dot, which only the minimap draws. */
function isQuietDone(glyph: Glyph): boolean {
  return glyph.shape === 'done' && !glyph.failed
}

interface ShapeProps {
  cx: number
  cy: number
  r: number
  glyph: Glyph
  /** A run's counts: a running or queued run's ring fills clockwise as its invocations finish. */
  counts?: Counts | null
  /** A spinner while running, unless the ring shows progress: for glyphs that summarize (a card,
   *  a row, a level), not every cell. */
  spin?: boolean
  /** Drawn faintly (its own colour, lower opacity), to set off the glyphs that are not muted. */
  muted?: boolean
  /** Running as a solid dot in ink rather than a progress ring: one step's row in the minimap. */
  runningDot?: boolean
}

/**
 * A run state as an element of an enclosing `<svg>`: shape is the state, colour only failure.
 * Waiting is a dashed ring, queued a ring, running a ring in ink (a spinner with `spin`), done a
 * solid dot, failed a ×, stopped a ring with a bar. A run's ring doubles as its progress:
 * a light track with an arc for the share of its invocations that have finished. With
 * `runningDot`, running is a solid dot in ink instead.
 */
export function GlyphShape({ cx: x, cy: y, r, glyph, counts, spin = false, muted = false, runningDot = false }: ShapeProps) {
  const { shape } = glyph
  const tone = styles[toneOf(glyph)]
  const stroke = Math.max(1, r * 0.3)
  const ringR = r - stroke / 2
  const ring = { cx: x, cy: y, r: ringR, fill: 'none', stroke: 'currentColor', strokeWidth: stroke }
  const progress =
    counts && counts.total > 1 && (shape === 'running' || shape === 'queued') ? settledFraction(counts) : null
  const outline =
    progress === null ? (
      <circle {...ring} />
    ) : (
      <>
        <circle {...ring} className={styles.track} />
        <ProgressArc x={x} y={y} r={ringR} fraction={progress} strokeWidth={stroke} />
      </>
    )
  let body: React.ReactNode
  switch (shape) {
    case 'waiting':
      body = <circle {...ring} strokeDasharray={`${r * 0.55} ${r * 0.45}`} />
      break
    case 'queued':
      body = outline
      break
    case 'running': {
      if (runningDot) body = <circle cx={x} cy={y} r={r} fill="currentColor" />
      else if (spin && progress === null) body = <Spinner x={x} y={y} r={ringR} strokeWidth={stroke} />
      else body = outline
      break
    }
    case 'done':
      body = <circle cx={x} cy={y} r={r} fill="currentColor" />
      break
    case 'failed': {
      const d = r * 0.75
      body = (
        <path
          d={`M ${x - d} ${y - d} L ${x + d} ${y + d} M ${x + d} ${y - d} L ${x - d} ${y + d}`}
          stroke="currentColor"
          strokeWidth={stroke * 1.25}
          strokeLinecap="round"
        />
      )
      break
    }
    case 'stopped':
      body = (
        <>
          <circle {...ring} />
          <line x1={x - r * 0.45} x2={x + r * 0.45} y1={y} y2={y} stroke="currentColor" strokeWidth={stroke} />
        </>
      )
      break
  }
  return (
    <g className={muted ? `${tone} ${styles.muted}` : tone}>
      <title>{glyphLabel(glyph)}</title>
      {body}
    </g>
  )
}

/** Three quarters of a ring turning about its centre, as the viewer's other loading spinners. */
function Spinner({ x, y, r, strokeWidth }: { x: number; y: number; r: number; strokeWidth: number }) {
  return (
    <path
      className={styles.spinner}
      style={{ transformOrigin: `${x}px ${y}px` }}
      d={arcPath(x, y, r, 0.75)}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
    />
  )
}

/** The finished share of a run, clockwise from the top of its ring. */
function ProgressArc({ x, y, r, fraction, strokeWidth }: { x: number; y: number; r: number; fraction: number; strokeWidth: number }) {
  if (fraction <= 0) return null
  if (fraction >= 1) return <circle cx={x} cy={y} r={r} fill="none" stroke="currentColor" strokeWidth={strokeWidth} />
  return <path d={arcPath(x, y, r, fraction)} fill="none" stroke="currentColor" strokeWidth={strokeWidth} />
}

interface Props {
  glyph: Glyph
  counts?: Counts | null
  spin?: boolean
  r?: number
  /**
   * Keep the glyph's room when it draws nothing, so labels in a list stay aligned. A glyph outside
   * the minimap draws nothing for done: done is the normal case, and says nothing.
   */
  reserve?: boolean
}

/**
 * The glyph on its own, inline with text (card heads, chips, the picker). It draws
 * nothing for something done without failures; only the minimap shows done dots.
 */
export function StateGlyph({ glyph, counts, spin = false, r = 4.5, reserve = false }: Props) {
  const size = r * 2 + 1
  if (isQuietDone(glyph)) return reserve ? <span className={styles.reserve} style={{ width: size }} /> : null
  return (
    <svg className={styles.glyph} width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img">
      <GlyphShape cx={size / 2} cy={size / 2} r={r} glyph={glyph} counts={counts} spin={spin} />
    </svg>
  )
}

/** `GlyphShape` for an SVG outside the minimap (the flowchart): nothing for done, as `StateGlyph`. */
export function QuietGlyphShape(props: ShapeProps) {
  return isQuietDone(props.glyph) ? null : <GlyphShape {...props} />
}
