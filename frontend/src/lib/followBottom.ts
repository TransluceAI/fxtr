import { useLayoutEffect, type RefObject } from 'react'

/** How close to its bottom, in pixels, a scroller counts as at the bottom. */
export const NEAR_BOTTOM = 24

/**
 * Keep a scroller at its bottom while its content grows, as a chat window does, for content that
 * streams in (a running step's preview): it starts at the bottom, and each time `contentRef`'s
 * element changes size the scroller moves to the bottom again, before the change is painted, if
 * it was at the bottom (within `NEAR_BOTTOM`) when last scrolled. Scrolling up stops it following
 * and scrolling back down starts it again; growth alone never decides, since it moves no scroll
 * position. With `follow` false it only starts at the bottom, once.
 */
export function useFollowBottom(
  scrollerRef: RefObject<HTMLElement | null>,
  contentRef: RefObject<HTMLElement | null>,
  follow: boolean,
) {
  useLayoutEffect(() => {
    const scroller = scrollerRef.current
    if (scroller) scroller.scrollTop = scroller.scrollHeight
  }, [scrollerRef])
  useLayoutEffect(() => {
    const scroller = scrollerRef.current
    const content = contentRef.current
    if (!follow || !scroller || !content) return
    let atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= NEAR_BOTTOM
    const onScroll = () => {
      atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= NEAR_BOTTOM
    }
    const observer = new ResizeObserver(() => {
      if (atBottom) scroller.scrollTop = scroller.scrollHeight
    })
    observer.observe(content)
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      observer.disconnect()
      scroller.removeEventListener('scroll', onScroll)
    }
  }, [scrollerRef, contentRef, follow])
}
