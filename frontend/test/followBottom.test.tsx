// @vitest-environment jsdom
import { act, useRef } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { NEAR_BOTTOM, useFollowBottom } from '../src/lib/followBottom'

const CLIENT_HEIGHT = 200
let contentHeight = 1000
let resized = () => {}
const tops = new WeakMap<Element, number>()
let host: HTMLDivElement
let root: Root

function Box({ live }: { live: boolean }) {
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  useFollowBottom(scroller, content, live)
  return <div ref={scroller}><div ref={content} /></div>
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resized = callback }
    observe() {}
    disconnect() { resized = () => {} }
  })
  // A scroller over content of `contentHeight`, clamping its scroll position as a browser does.
  vi.spyOn(Element.prototype, 'scrollHeight', 'get').mockImplementation(() => contentHeight)
  vi.spyOn(Element.prototype, 'clientHeight', 'get').mockReturnValue(CLIENT_HEIGHT)
  vi.spyOn(Element.prototype, 'scrollTop', 'get').mockImplementation(function (this: Element) { return tops.get(this) ?? 0 })
  vi.spyOn(Element.prototype, 'scrollTop', 'set').mockImplementation(function (this: Element, top: number) {
    tops.set(this, Math.max(0, Math.min(top, contentHeight - CLIENT_HEIGHT)))
  })
  contentHeight = 1000
  resized = () => {}
  host = document.createElement('div')
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const scroller = () => host.firstElementChild!
const bottom = () => contentHeight - CLIENT_HEIGHT

/** The content grows to `height`, as a new preview renders. */
function grow(height: number) {
  contentHeight = height
  act(() => resized())
}

/** Scroll by hand to `top`. */
function scrollTo(top: number) {
  scroller().scrollTop = top
  act(() => { scroller().dispatchEvent(new Event('scroll')) })
}

it('opens at the bottom and stays there as a live preview grows', async () => {
  await act(async () => root.render(<Box live />))
  expect(scroller().scrollTop).toBe(bottom())
  grow(1500)
  expect(scroller().scrollTop).toBe(1300)
})

it('stops following when scrolled up, and follows again once back near the bottom', async () => {
  await act(async () => root.render(<Box live />))
  scrollTo(300)
  grow(1500)
  expect(scroller().scrollTop).toBe(300)
  scrollTo(bottom() - NEAR_BOTTOM)
  grow(2000)
  expect(scroller().scrollTop).toBe(bottom())
})

it('opens a stopped step\'s preview at the bottom without following it', async () => {
  await act(async () => root.render(<Box live={false} />))
  expect(scroller().scrollTop).toBe(800)
  grow(1500)
  expect(scroller().scrollTop).toBe(800)
})

it('stops following when the step stops', async () => {
  await act(async () => root.render(<Box live />))
  await act(async () => root.render(<Box live={false} />))
  grow(1500)
  expect(scroller().scrollTop).toBe(800)
})
