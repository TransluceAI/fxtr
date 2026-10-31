import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { isEditableTarget } from './keyboard'

/** How long a card is held still after a slice change, at most: long enough for every card's
 *  table to read the new slice. */
const HOLD_MS = 10_000

/** Keys that scroll the trace, and so let go of a held card. */
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '])

export interface CardBox { id: string; top: number; bottom: number }

/** The card to hold still when a change was not made in one: the first whose top is on screen, or
 *  failing that the one across the top of the viewport. Holding a card whose top is scrolled off
 *  would move what is being looked at below it by however much that card changes. */
export function defaultAnchor(cards: readonly CardBox[], viewTop: number, viewBottom: number): string | null {
  const starting = cards.find((card) => card.top >= viewTop && card.top < viewBottom)
  if (starting) return starting.id
  return cards.find((card) => card.top < viewTop && card.bottom > viewTop)?.id ?? null
}

/**
 * Hold one card of the trace still on screen while the cards around it change height (a slice
 * change, whose tables load their new rows when they arrive): `hold(id)` notes where the card with
 * `data-card-id={id}` is in the scroller (the default card when no id is given, `defaultAnchor`),
 * and every time a card, or the level, changes size the scroller is moved to put it back, before
 * the change is painted. It lets go when the trace is scrolled by hand (wheel, touch, scrollbar,
 * keys), another card is held, or after `HOLD_MS`.
 */
export function useScrollAnchor(scrollRef: RefObject<HTMLElement>, cardsRef: RefObject<HTMLElement>) {
  const held = useRef<(() => void) | null>(null)
  useEffect(() => () => held.current?.(), [])
  return useCallback((id?: string) => {
    held.current?.()
    const scroller = scrollRef.current
    const cards = cardsRef.current
    if (!scroller || !cards) return
    const rows = Array.from(cards.querySelectorAll<HTMLElement>(':scope > [data-card-id]'))
    const viewTop = scroller.getBoundingClientRect().top
    const anchorId = id ?? defaultAnchor(rows.map((row) => {
      const box = row.getBoundingClientRect()
      return { id: row.dataset.cardId!, top: box.top, bottom: box.bottom }
    }), viewTop, viewTop + scroller.clientHeight)
    const anchor = rows.find((row) => row.dataset.cardId === anchorId)
    if (!anchor) return
    const offset = anchor.getBoundingClientRect().top - viewTop
    const restore = () => {
      if (!anchor.isConnected) return
      const drift = anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top - offset
      if (Math.abs(drift) >= 0.5) scroller.scrollTop += drift
    }
    const observer = new ResizeObserver(restore)
    if (cards.parentElement) observer.observe(cards.parentElement)
    for (const row of rows) observer.observe(row)
    const onKeyDown = (event: KeyboardEvent) => {
      if (SCROLL_KEYS.has(event.key) && !isEditableTarget(event.target)) release()
    }
    // A press on the scroller itself, not on anything in it, is on its scrollbar.
    const onPointerDown = (event: PointerEvent) => {
      if (event.target === scroller) release()
    }
    const timer = setTimeout(release, HOLD_MS)
    scroller.addEventListener('wheel', release, { passive: true })
    scroller.addEventListener('touchmove', release, { passive: true })
    scroller.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('keydown', onKeyDown)
    function release() {
      observer.disconnect()
      clearTimeout(timer)
      scroller!.removeEventListener('wheel', release)
      scroller!.removeEventListener('touchmove', release)
      scroller!.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('keydown', onKeyDown)
      if (held.current === release) held.current = null
    }
    held.current = release
  }, [scrollRef, cardsRef])
}
