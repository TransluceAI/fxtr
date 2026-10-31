import { useEffect, useState } from 'react'
import { ChevronsLeft, ChevronsRight, ChevronLeft, ChevronRight, LoaderCircle } from 'lucide-react'

export const PAGE_SIZE = 25

/** A 1-based row number selects the aligned page containing that row. */
export function rowOffset(row: number, total: number | null, pageSize: number): number {
  // GraphQL Int offsets are signed 32-bit integers.
  const bounded = Math.max(1, Math.min(row, total ?? 2_147_483_647, 2_147_483_647))
  return Math.floor((bounded - 1) / pageSize) * pageSize
}

/** Where the next (1) or previous (-1) page starts, from the page at `offset`; null past either
 *  end. */
export function adjacentOffset({ offset, hasMore }: { offset: number; hasMore: boolean }, pageSize: number, direction: 1 | -1): number | null {
  if (direction === 1) return hasMore ? offset + pageSize : null
  return offset > 0 ? Math.max(0, offset - pageSize) : null
}

/** A compact editable range and page navigation, shared by output and dimension tables. */
export function Pagination({ offset, count, total, hasMore, loading, refreshing = false, disabled = false, pageSize = PAGE_SIZE, onPage, onLast }: {
  offset: number; count: number; total: number | null; hasMore: boolean; loading: boolean; refreshing?: boolean; disabled?: boolean;
  pageSize?: number; onPage: (offset: number) => void; onLast: () => void
}) {
  const start = count ? (offset + 1).toLocaleString() : '0'
  const [draft, setDraft] = useState(start)
  useEffect(() => setDraft(start), [start])
  const rest = <>
    {count > 0 && <>–{(offset + count).toLocaleString()}</>}
    {total !== null && <> <span className="pagination-total">/ {total.toLocaleString()}</span></>}
  </>
  const busy = loading || disabled
  const previous = adjacentOffset({ offset, hasMore }, pageSize, -1)
  const next = adjacentOffset({ offset, hasMore }, pageSize, 1)
  const commit = () => {
    if (busy || total === 0) return
    const text = draft.replace(/[,\s]/g, '')
    if (!/^[+-]?\d+$/.test(text)) { setDraft(start); return }
    const next = rowOffset(Number(text), total, pageSize)
    setDraft(start)
    if (next !== offset) onPage(next)
  }
  return <nav className="pagination" aria-label="Pagination" aria-busy={loading || refreshing}>
    {(loading || refreshing) && <span className="pagination-loading" role="status" aria-label="Loading page"><LoaderCircle size={12} /></span>}
    <span className="pagination-range">
      <input className="pagination-position" aria-label="Go to row" inputMode="numeric" value={draft}
        title="Go to row (1-based). Enter a number to open the page containing it."
        style={{ width: `${Math.max(1, draft.length)}ch` }} disabled={busy || total === 0}
        onFocus={(event) => event.currentTarget.select()} onChange={(event) => setDraft(event.target.value)} onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); commit() }
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setDraft(start) }
        }} />
      {rest}
    </span>
    <button aria-label="First page" title="First page" disabled={offset === 0 || busy} onClick={() => onPage(0)}><ChevronsLeft size={13} /></button>
    <button aria-label="Previous page" title="Previous page" disabled={previous === null || busy} onClick={() => onPage(previous!)}><ChevronLeft size={13} /></button>
    <button aria-label="Next page" title="Next page" disabled={next === null || busy} onClick={() => onPage(next!)}><ChevronRight size={13} /></button>
    <button aria-label="Last page" title="Last page" disabled={!hasMore || busy} onClick={onLast}><ChevronsRight size={13} /></button>
  </nav>
}
