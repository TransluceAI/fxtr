import { useMemo, useRef, useState } from 'react'
import { SlidersHorizontal, X } from 'lucide-react'
import { levelDimList, levelDims, withoutDim, type LevelDim } from '../lib/keyLists'
import { useDismiss } from '../lib/dismiss'
import { useLevelState } from '../lib/levelState'
import { cx } from '../lib/cx'
import type { StepDoc } from '../types'
import { KeyValueList } from './KeyValueList'
import styles from './SliceDims.module.css'

/** What the button says: `slice dims`, or how many dims the level's slice is on. */
export function sliceLabel(count: number): string {
  return count === 0 ? 'slice dims' : `sliced on ${count} ${count === 1 ? 'dim' : 'dims'}`
}

/**
 * The level's slice control, floated over the top right of the trace (or the flowchart): a button
 * naming how many dims the level's slice is on (with an × beside it clearing the slice while it is
 * on any), opening a panel with every dim of the
 * level's nodes (whether a node maps over it is a property of the node, so none is coloured by
 * kind). The left column lists the dims, each with its picked value below its name; the right lists
 * the values of the dim chosen on the left (`levelDims` says where they are read), the ones the
 * other picks rule out on that source dimmed but still choosable. While the slice is on any dim, a
 * `clear all` button pinned under the dims empties it. The panel closes on a click
 * outside it, Escape, or the button. Nothing is shown for a level whose nodes have no dims.
 */
export function SliceDims({ steps, className, panelAlign = 'right' }: {
  steps: readonly StepDoc[]
  className?: string
  /** Which edge of the button the panel lines up with: `left` for a control that may sit at the
   *  left edge of the screen. */
  panelAlign?: 'left' | 'right'
}) {
  const { slice, setSlice } = useLevelState()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const dims = useMemo(() => levelDims(steps), [steps])
  const count = Object.keys(slice).length

  useDismiss(rootRef, open, () => setOpen(false))

  if (dims.length === 0) return null
  return (
    <div className={cx(styles.root, className)} ref={rootRef}>
      <div className={styles.buttons}>
        <button className={cx(styles.button, count > 0 && styles.isSliced)} aria-expanded={open} onClick={() => setOpen(!open)}>
          <SlidersHorizontal size={13} />
          {sliceLabel(count)}
        </button>
        {count > 0 && (
          <button className={cx(styles.button, styles.isSliced, styles.clearButton)} aria-label="Clear the slice"
            title="clear all" onClick={() => setSlice({})}>
            <X size={13} />
          </button>
        )}
      </div>
      {open && <SliceDimsPanel steps={steps} className={cx(styles.floating, panelAlign === 'left' && styles.alignLeft)} />}
    </div>
  )
}

/**
 * The slice panel's body: every dim of `steps` on the left, each with its picked value under its
 * name and `clear all` pinned at the foot while the slice is on any dim, and the chosen dim's
 * values on the right. It edits the slice of the nearest `LevelStateContext`, so a panel pinned to
 * its own slice provides that one around it.
 */
export function SliceDimsPanel({ steps, className }: { steps: readonly StepDoc[]; className?: string }) {
  const { slice, setSlice } = useLevelState()
  const [activeDim, setActiveDim] = useState<string | null>(null)
  const dims = useMemo(() => levelDims(steps), [steps])
  const active = dims.find((entry) => entry.dim === activeDim) ?? dims[0] ?? null
  const count = Object.keys(slice).length
  if (dims.length === 0) return <div className={cx(styles.panel, styles.empty, className)}>no dims to slice</div>
  return (
    <div className={cx(styles.panel, className)} role="dialog" aria-label="Slice dims">
      <div className={styles.dimsColumn}>
        <div className={styles.dims}>
          {dims.map((entry) => {
            const picked = slice[entry.dim]
            return (
              <button key={entry.dim} className={cx(styles.dim, entry === active && styles.isActive)}
                aria-pressed={entry === active} onClick={() => setActiveDim(entry.dim)}>
                <span className={styles.dimName}>{entry.dim}</span>
                <span className={cx(styles.dimPick, picked !== undefined && styles.isPicked)}>
                  {/* A no-break space: a plain one collapses, and the line would lose its height. */}
                  {picked === undefined ? '\u00a0' : String(picked)}
                </span>
              </button>
            )
          })}
        </div>
        {count > 0 && <button className={styles.clearAll} onClick={() => setSlice({})}>clear all</button>}
      </div>
      <div className={styles.values}>{active && <DimValues entry={active} />}</div>
    </div>
  )
}

function DimValues({ entry }: { entry: LevelDim }) {
  const { slice, setSlice } = useLevelState()
  const list = levelDimList(entry, slice)
  if (!list) return <span className={styles.none}>no node has output with {entry.dim} yet</span>
  return (
    <KeyValueList key={list.scope} dim={entry.dim} scope={list.scope} against={list.against} load={list.load}
      version={list.version} enabled picked={slice[entry.dim]} chooseUnavailable fill
      onChoose={(value) => setSlice({ ...slice, [entry.dim]: value })}
      onClear={() => setSlice(withoutDim(slice, entry.dim))} />
  )
}
