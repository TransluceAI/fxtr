// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Flowchart } from '../src/components/Flowchart'
import { buildLevel } from '../src/model/level'
import { inProgress, overviewOf } from './fixtures'

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1200)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(800)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function renderGraph() {
  const level = buildLevel('job', overviewOf(inProgress())).steps
  render(<Flowchart levelKey="job" level={level} selectedId={null} onSelect={() => {}} />)
  const viewport = screen.getByRole('region', { name: 'Workflow flowchart' }).firstElementChild!
  const transform = () => viewport.querySelector('svg > g')!.getAttribute('transform')!
  const scale = () => transform().match(/scale\(([^)]+)\)/)![1]
  const wheel = (modifiers: MouseEventInit) => fireEvent.wheel(viewport, {
    deltaY: 50, clientX: 200, clientY: 200, ...modifiers,
  })
  return { transform, scale, wheel }
}

it.each(['MacIntel', 'Win32', 'Linux x86_64'])('zooms only with the platform modifier on %s', (platform) => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform)
  const modifier = platform === 'MacIntel' ? { metaKey: true } : { ctrlKey: true }
  const wrong = platform === 'MacIntel' ? { ctrlKey: true } : { metaKey: true }
  const { transform, scale, wheel } = renderGraph()
  const initialScale = scale()
  for (const modifiers of [{}, wrong, { metaKey: true, ctrlKey: true }]) {
    fireEvent.keyDown(window, modifiers)
    const before = transform()
    wheel(modifiers)
    expect(scale()).toBe(initialScale)
    expect(transform()).not.toBe(before)
    fireEvent.keyUp(window)
  }
  fireEvent.keyDown(window, modifier)
  wheel(modifier)
  expect(scale()).not.toBe(initialScale)
})

it('keeps Mac trackpad pinch zoom after Control is released or the window loses focus', () => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue('MacIntel')
  const { scale, wheel } = renderGraph()
  let before = scale()
  wheel({ ctrlKey: true }) // A pinch has no preceding physical Control key event.
  expect(scale()).not.toBe(before)
  fireEvent.keyDown(window, { key: 'Control', ctrlKey: true })
  before = scale()
  wheel({ ctrlKey: true })
  expect(scale()).toBe(before)
  fireEvent.keyUp(window, { key: 'Control' })
  wheel({ ctrlKey: true })
  expect(scale()).not.toBe(before)
  fireEvent.keyDown(window, { key: 'Control', ctrlKey: true })
  fireEvent.blur(window)
  before = scale()
  wheel({ ctrlKey: true })
  expect(scale()).not.toBe(before)
})
