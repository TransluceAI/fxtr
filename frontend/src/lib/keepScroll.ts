import { useEffect, useRef, type RefObject } from 'react'

/** The scroll areas inside a panel whose positions are kept: its body's, and a table's. */
const SCROLLERS = '.detail-scroll, .table-scroll'

/** How long after its contents change a panel keeps putting its scroll back, in ms: the new
 *  contents load in stages, and their scroll areas only grow tall enough as they do. */
const RESTORE_MS = 3000

/**
 * Keep the scroll of the scroll areas inside `root` across a change of what it shows (`showing`
 * changes: a parent's click navigated the panel, or its slice changed). The scroll areas are
 * replaced as the new contents load (a placeholder in between), so positions are kept by each
 * area's place among them (the first, the second, …), recorded as the user scrolls, and put back on
 * the new ones as they appear and grow tall enough, until the user scrolls, presses, or types in the
 * panel, or `RESTORE_MS` passes.
 */
export function useKeepScroll(root: RefObject<HTMLElement | null>, showing: string) {
  const positions = useRef(new Map<number, { top: number; left: number }>())
  const restoring = useRef(false)

  // Record positions the user scrolls to (not ones put back here).
  useEffect(() => {
    const element = root.current
    if (!element) return
    const onScroll = (event: Event) => {
      if (restoring.current) return
      const target = event.target as HTMLElement
      if (!target.matches?.(SCROLLERS)) return
      const index = scrollers(element).indexOf(target)
      if (index >= 0) positions.current.set(index, { top: target.scrollTop, left: target.scrollLeft })
    }
    element.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => element.removeEventListener('scroll', onScroll, { capture: true })
  }, [root])

  // On a change of what it shows, put the positions back as the new scroll areas appear and grow.
  const first = useRef(true)
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    const element = root.current
    if (!element || positions.current.size === 0) return
    restoring.current = true
    const apply = () => {
      const areas = scrollers(element)
      for (const [index, { top, left }] of positions.current) {
        const area = areas[index]
        if (!area) continue
        if (Math.abs(area.scrollTop - top) > 1) area.scrollTop = top
        if (Math.abs(area.scrollLeft - left) > 1) area.scrollLeft = left
      }
    }
    const stop = () => {
      restoring.current = false
      observer.disconnect()
      window.clearTimeout(timer)
      window.clearInterval(tick)
      for (const type of ['wheel', 'pointerdown', 'keydown'] as const) element.removeEventListener(type, stop, true)
    }
    const observer = new MutationObserver(apply)
    observer.observe(element, { childList: true, subtree: true, attributes: true, characterData: true })
    const timer = window.setTimeout(stop, RESTORE_MS)
    // A scroll area can also grow without anything inside it changing (its panel's cap lifting).
    const tick = window.setInterval(apply, 100)
    for (const type of ['wheel', 'pointerdown', 'keydown'] as const) element.addEventListener(type, stop, true)
    apply()
    return stop
  }, [root, showing])
}

function scrollers(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(SCROLLERS))
}
