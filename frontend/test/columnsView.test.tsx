// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {dispatchRegistry} from 'fxtr-view-shell-util'
import {buildLevel} from '../src/model/level'
import {parseRel} from '../src/panes/address'
import {TRACE} from '../src/panes/columns'
import {ColumnsView} from '../src/panes/ColumnsView'
import {useColumns, type Centre, type ColumnsState} from '../src/panes/useColumns'
import type {Catalog} from '../src/panes/resolve'
import {inProgress, overviewOf} from './fixtures'

const level = buildLevel('job', overviewOf(inProgress()))
const catalog: Catalog = {root: 'root', registry: dispatchRegistry([]), level: () => ({status: 'ready', level})}
const centre: Centre = {address: {view: 'trace', location: {root: 'step', steps: [], path: []}}, keys: {}}

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
let state: ColumnsState
let frames: FrameRequestCallback[]
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', () => ({matches: true}))
  vi.stubGlobal('CSS', {escape: (text: string) => text})
  // jsdom computes no padding, which the panels' heights are read against.
  vi.stubGlobal('getComputedStyle', () => ({paddingTop: '0px', paddingBottom: '0px'}))
  frames = []
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback))
  vi.stubGlobal('cancelAnimationFrame', () => {})
  host = document.createElement('div')
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
})

it('slides the columns on a sideways scroll, once a frame, without rendering any panel again', async () => {
  const rendered = vi.fn()
  const Panel = ({id}: {id: string}) => {
    rendered(id)
    return <span>{id}</span>
  }
  const slides: number[] = []
  const onSlide = (slide: number) => {
    slides.push(slide)
  }
  function Harness() {
    state = useColumns({}, catalog, centre)
    return <ColumnsView state={state} onSlide={onSlide} renderPanel={(panel) => <Panel id={panel.id} />} />
  }
  await act(async () => root.render(<Harness />))
  await act(async () => state.follow({rel: parseRel('(/roll)').rel, params: {}}, TRACE))
  await act(async () => state.follow({rel: parseRel('(/grade)').rel, params: {}}, TRACE, {pin: true}))
  rendered.mockClear()
  const frame = host.querySelector<HTMLElement>('[style*="left"]')!
  const wheel = (deltaX: number) => frame.dispatchEvent(new WheelEvent('wheel', {deltaX, cancelable: true, bubbles: true}))
  await act(async () => {
    wheel(30)
    wheel(20)
  })
  expect(frames).toHaveLength(1)
  await act(async () => frames.shift()!(0))
  expect(frame.style.left).toBe('-50px')
  expect(slides.at(-1)).toBe(50)
  expect(rendered).not.toHaveBeenCalled()
})
