import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { LoaderCircle, X } from 'lucide-react'
import { useQuery } from '../api/browse'
import { cx } from '../lib/cx'
import { useDebouncedInput } from '../lib/debounce'
import { stepKey } from '../lib/tableKeys'
import type { KeyList, KeyListItem } from '../lib/keyLists'
import type { KeyValue } from '../types'
import styles from './KeyValueList.module.css'

const VALUE_ROW_H = 27
const MAX_VISIBLE_ROWS = 10
/** Rows drawn beyond each edge of the viewport, so a fast scroll does not show blank space. */
const OVERSCAN_ROWS = 5

/** The value a dim takes, keyed so that `1` and `"1"` stay distinct. */
function valueToken(value: KeyValue): string {
  return `${typeof value}:${String(value)}`
}

interface Props {
  dim: string
  /** Names where the values come from (source and dim). Search and scroll are kept while it stays. */
  scope: string
  /** Names what compatibility is judged against; a change refreshes the values in place. */
  against: string
  load: (search: string, signal: AbortSignal) => Promise<KeyList>
  version?: string
  /** Query only while shown. */
  enabled: boolean
  picked: KeyValue | undefined
  onChoose: (value: KeyValue) => void
  onClear: () => void
  /** Let values the other picks rule out be chosen too (they stay dimmed). */
  chooseUnavailable?: boolean
  /** Fill the container's height instead of sizing to at most ten values. */
  fill?: boolean
  /** The pick is a selection (what an open panel shows), not a slice: drawn in selection blue. */
  selection?: boolean
}

/**
 * One dim's values, the first `KEY_LIST_LIMIT` of those matching the search, read from the server:
 * the ones the other picks rule out dimmed.
 * Only the rows in view are drawn; when more values match than were read, the list ends in a "more
 * not shown" row, and searching is how to reach them. The list is headed by its search box while no
 * value is picked, and by a `clear` cell of the same height in its place while one is (it stays put
 * as the values scroll); the search still applies while hidden. Clicking the picked value again
 * clears it too.
 * j/k step through the values while the pointer is over the list, scrolling the pick into view. A
 * change of `against` keeps the search, scroll, and rows while compatibility reloads.
 */
export function KeyValueList({ dim, scope, against, load, version, enabled, picked, onChoose, onClear, chooseUnavailable = false, fill = false, selection = false }: Props) {
  const [needle, setNeedle] = useState('')
  const { value: search, set: setSearch } = useDebouncedInput(needle, setNeedle)
  const [hovered, setHovered] = useState(false)
  const valuesRef = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(0)
  const queryScope = JSON.stringify([scope, needle])
  const query = useQuery(JSON.stringify(['key-values', queryScope, against]),
    (signal) => load(needle, signal), { enabled, retainKey: queryScope, version })
  useEffect(() => {
    if (valuesRef.current) valuesRef.current.scrollTop = 0
    setScrollTop(0)
  }, [queryScope])
  useLayoutEffect(() => {
    const values = valuesRef.current!
    const measure = () => setViewportHeight(values.clientHeight)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(values)
    return () => observer.disconnect()
  }, [])
  const choosable = (item: KeyListItem) => item.compatible || chooseUnavailable
  useEffect(() => {
    if (!hovered || !enabled || query.loading || query.error) return
    const all = query.value?.items ?? []
    const items = all.filter(choosable)
    if (!items.length) return
    const onKeyDown = (event: KeyboardEvent) => {
      const direction = stepKey(event)
      if (direction === null) return
      const current = items.findIndex((item) => item.value === picked)
      const next = current < 0 ? direction === 1 ? 0 : items.length - 1
        : Math.max(0, Math.min(items.length - 1, current + direction))
      event.preventDefault()
      revealRow(valuesRef.current!, all.indexOf(items[next]))
      onChoose(items[next].value)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [hovered, enabled, query.value, query.loading, query.error, picked, onChoose, chooseUnavailable])
  const items = query.value?.items ?? []
  const hasMore = !!query.value?.hasMore
  const height = fill ? undefined : Math.min(MAX_VISIBLE_ROWS, Math.max(1, items.length + (hasMore ? 1 : 0))) * VALUE_ROW_H
  // Before the viewport is measured (or where layout is not run), draw what a full list shows.
  const shownHeight = viewportHeight || height || MAX_VISIBLE_ROWS * VALUE_ROW_H
  const start = Math.max(0, Math.floor(scrollTop / VALUE_ROW_H) - OVERSCAN_ROWS)
  const end = Math.min(items.length, Math.ceil((scrollTop + shownHeight) / VALUE_ROW_H) + OVERSCAN_ROWS)
  return (
    <section className={cx(styles.list, fill && styles.fill)} aria-label={`${dim} keys`} onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}>
      {/* One slot, so picking or clearing a value never moves the values below it. */}
      <div className={cx(styles.headRow, picked !== undefined && styles.isPicked)}>
        {picked === undefined ? (
          <input className={styles.search} aria-label={`Search ${dim}`} placeholder={`Search ${dim}…`} value={search} onChange={(e) => setSearch(e.target.value)} />
        ) : (
          <button className={styles.clearCell} onClick={onClear} aria-label={`Clear ${dim} slice`} title={`Clear ${dim}: ${String(picked)}`}>
            <X size={12} />
            clear
          </button>
        )}
        <span className={styles.loading}>{enabled && (query.loading || query.refreshing) && <LoaderCircle size={12} role="status" aria-label={`Loading ${dim} keys`} />}</span>
      </div>
      <div ref={valuesRef} className={styles.values} aria-busy={query.loading || query.refreshing} style={{ height }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
        {query.error ? <button onClick={query.reload}>retry keys</button> : query.loading && !query.value ? <span>loading…</span> : null}
        <div style={{ height: items.length * VALUE_ROW_H, paddingTop: start * VALUE_ROW_H }}>
          {items.slice(start, end).map((item) => <KeyValueCell key={valueToken(item.value)} value={item.value}
            selected={picked === item.value} compatible={item.compatible} choosable={choosable(item)} pending={query.loading || !!query.error}
            selection={selection} onSelect={() => onChoose(item.value)} onClear={onClear} />)}
        </div>
        {hasMore && <div className={styles.more}>more not shown</div>}
      </div>
    </section>
  )
}

/** Scroll the list's values just enough to bring the row at `index` fully into view. */
function revealRow(values: HTMLElement, index: number) {
  const top = index * VALUE_ROW_H
  if (top < values.scrollTop) values.scrollTop = top
  else if (top + VALUE_ROW_H > values.scrollTop + values.clientHeight) values.scrollTop = top + VALUE_ROW_H - values.clientHeight
}

interface CellProps {
  value: KeyValue
  selected: boolean
  /** Whether the other picks leave the value possible; one they rule out is dimmed. */
  compatible: boolean
  choosable: boolean
  pending: boolean
  /** Drawn, when selected, in selection blue. */
  selection: boolean
  onSelect: () => void
  /** Clicking the selected value clears the dim's pick. */
  onClear: () => void
}

/** One value; when its label is clipped, hovering floats the full label over the cell. */
function KeyValueCell({ value, selected, compatible, choosable, pending, selection, onSelect, onClear }: CellProps) {
  const buttonRef = useRef<HTMLButtonElement>(null)
  const labelRef = useRef<HTMLSpanElement>(null)
  const [float, setFloat] = useState<{ left: number; top: number; width: number } | null>(null)
  const label = String(value)

  const showFullValue = () => {
    const button = buttonRef.current
    const text = labelRef.current
    if (!button || !text || text.scrollWidth <= text.clientWidth) return
    const box = button.getBoundingClientRect()
    setFloat({ left: box.left, top: box.top, width: box.width })
  }

  return (
    <>
      <button
        ref={buttonRef}
        className={cx(styles.value, selected && styles.isSelected, selected && selection && styles.isSelection, !compatible && styles.isUnavailable, choosable && styles.isChoosable)}
        aria-pressed={selected}
        aria-disabled={!selected && (!choosable || pending)}
        onClick={selected ? onClear : choosable && !pending ? onSelect : undefined}
        onMouseEnter={showFullValue}
        onMouseLeave={() => setFloat(null)}
      >
        <span ref={labelRef} className={styles.valueLabel}>
          {label}
        </span>
      </button>
      {float &&
        createPortal(
          <div
            className={cx(styles.float, selected && styles.isSelected, selected && selection && styles.isSelection, !compatible && styles.isUnavailable)}
            style={{ left: float.left, top: float.top, minWidth: float.width }}
          >
            {label}
          </div>,
          document.body,
        )}
    </>
  )
}
