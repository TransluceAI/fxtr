import { useEffect, useRef } from 'react'
import type { Finish } from '../api/finishes'
import styles from './Heartbeat.module.css'

/** The minute the heartbeat shows, ending now. */
export const HEARTBEAT_WINDOW_MS = 60_000
const WIDTH = 294
const HEIGHT = 26
const BLOCK = 2
/** A column is a block and a 1px gap. */
const COLUMN = BLOCK + 1
const COLUMN_MS = HEARTBEAT_WINDOW_MS / Math.floor(WIDTH / COLUMN)
/** Columns count from here, so their coordinates stay small. */
const ORIGIN = Date.now()

/** One column of the heartbeat: the finishes in its slice of time, oldest at the bottom. */
export interface HeartbeatColumn {
  index: number
  finishes: Finish[]
}

/**
 * What the heartbeat draws at `now`: every finish of the last minute (and a column's worth more, to
 * scroll in from the left) by column, and the block height. A column is a fixed `COLUMN_MS` of time,
 * so a finish never moves between columns. Blocks share the height among the tallest column's
 * finishes, one full-height block each while finishes are sparse; when the tallest column has more
 * finishes than the height has pixel pairs, `blockHeight` is null and columns are drawn as bars
 * instead, as tall as their share of the tallest.
 */
export function heartbeatLayout(finishes: readonly Finish[], now: number): { columns: HeartbeatColumn[]; blockHeight: number | null; tallest: number } {
  const byIndex = new Map<number, Finish[]>()
  for (const finish of finishes) {
    if (now - finish.at > HEARTBEAT_WINDOW_MS + COLUMN_MS) continue
    const index = Math.floor((finish.at - ORIGIN) / COLUMN_MS)
    const column = byIndex.get(index)
    if (column) column.push(finish)
    else byIndex.set(index, [finish])
  }
  const columns = [...byIndex].map(([index, list]) => ({ index, finishes: list }))
  const tallest = Math.max(1, ...columns.map((column) => column.finishes.length))
  const pitch = Math.floor(HEIGHT / tallest)
  const blockHeight = tallest === 1 ? HEIGHT - 1 : pitch >= 2 ? pitch - 1 : null
  return { columns, blockHeight, tallest }
}

/** Where the strip's columns sit at `now`, in whole pixels: the current column at the right edge. */
function offsetAt(now: number): number {
  return Math.round(WIDTH - ((now - ORIGIN) / COLUMN_MS) * COLUMN)
}

/**
 * A minute of a run's finishes: a 2px block per finish (red for a failure), stacked when finishes
 * share a column. It slides left a whole pixel at a time, from the page's animation frames between
 * polls (or only when it redraws, with reduced motion); sliding by fractions of a pixel shimmers.
 */
export function Heartbeat({ finishes }: { finishes: readonly Finish[] }) {
  const group = useRef<SVGGElement>(null)
  const now = Date.now()
  const { columns, blockHeight, tallest } = heartbeatLayout(finishes, now)
  useEffect(() => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return
    let frame = 0
    let shown = Number.NaN
    const step = () => {
      const offset = offsetAt(Date.now())
      if (offset !== shown && group.current) {
        group.current.setAttribute('transform', `translate(${offset} 0)`)
        shown = offset
      }
      frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [])
  const blocks: React.ReactNode[] = []
  for (const { index, finishes: list } of columns) {
    const x = index * COLUMN
    if (blockHeight !== null) {
      list.forEach((finish, level) => {
        const pitch = tallest === 1 ? HEIGHT : blockHeight + 1
        blocks.push(
          <rect key={`${index}-${level}`} x={x} y={HEIGHT - 1 - level * pitch - blockHeight} width={BLOCK} height={blockHeight}
            className={finish.failed ? styles.failed : styles.done} />,
        )
      })
    } else {
      const height = Math.max(1, Math.round((list.length / tallest) * (HEIGHT - 1)))
      const failedHeight = Math.round((list.filter((finish) => finish.failed).length / list.length) * height)
      blocks.push(<rect key={index} x={x} y={HEIGHT - 1 - height} width={BLOCK} height={height - failedHeight} className={styles.done} />)
      if (failedHeight > 0)
        blocks.push(<rect key={`${index}-failed`} x={x} y={HEIGHT - 1 - failedHeight} width={BLOCK} height={failedHeight} className={styles.failed} />)
    }
  }
  return (
    <div className={styles.heartbeat}>
      <svg width={WIDTH} height={HEIGHT} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} shapeRendering="crispEdges" role="img" aria-label="a block for each finish in the last minute">
        <line x1={0} x2={WIDTH} y1={HEIGHT - 0.5} y2={HEIGHT - 0.5} className={styles.baseline} />
        <g ref={group} transform={`translate(${offsetAt(now)} 0)`}>{blocks}</g>
      </svg>
      <div className={styles.caption}>
        <span>1 min ago</span>
        <span>now</span>
      </div>
    </div>
  )
}
