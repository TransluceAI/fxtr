// @vitest-environment jsdom
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {InvocationPanel, declarePage, declareView, page, renderer} from 'fxtr-view'
import {RegistryProvider, dispatchRegistry} from 'fxtr-view-shell-util'
import {LevelViews} from '../src/components/LevelViews'
import {LevelStateContext, type LevelState} from '../src/lib/levelState'

const Blank = () => null
const overview = declareView(InvocationPanel, 'test.run', 'test.Overview')
const fit = declareView(InvocationPanel, 'test.run', 'test.Fit', {params: ['point']})
const within = declareView(InvocationPanel, 'test.run', 'detail', {within: overview})
const dashboard = declarePage('test.dashboard')
const poet = declarePage('test.poet', {params: ['poet']})
const registry = dispatchRegistry([
  renderer(overview, Blank),
  renderer(fit, Blank),
  renderer(within, Blank),
  page(dashboard, Blank),
  page(poet, Blank),
])
const level = {levelPath: []} as unknown as LevelState

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

it('lists only the views that open without params, none declared within another, and no pages', async () => {
  await act(async () => root.render(
    <RegistryProvider registry={registry}>
      <LevelStateContext.Provider value={level}>
        <LevelViews workflowName="test.run" active={null} onChange={() => {}} />
      </LevelStateContext.Provider>
    </RegistryProvider>,
  ))
  const names = Array.from(host.querySelectorAll('button')).map((button) => button.textContent)
  expect(names).toEqual(['test.Overview'])
})
