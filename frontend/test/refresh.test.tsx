// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {expect, it, vi} from 'vitest'
import {levelState as aLevelState, overviewOf, recordingOpener} from './fixtures'
import {buildLevel} from '../src/model/level'
import {OpenEntityContext} from '../src/lib/openEntity'
vi.mock('fxtr-view-shell-util', async importOriginal => ({
  ...await importOriginal<typeof import('fxtr-view-shell-util')>(),
  useRenderers: () => [{id:'custom'}], chosenRenderer: () => 'custom',
  RenderInvocation: ({invocations}: {invocations: unknown[]}) => <div data-testid="custom-result">{invocations.length} loaded</div>,
}))
import {StepPanel} from '../src/components/StepPanel'
import {LevelStateContext} from '../src/lib/levelState'
it('allows a two-second custom view load to finish while overview revisions arrive', async () => {
  vi.useFakeTimers(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div'); const root = createRoot(host)
  const signals: AbortSignal[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    const {variables} = JSON.parse(init.body)
    signals.push(init.signal)
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, 1000)
      init.signal.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) })
    })
    const start = variables.after ? 200 : 0
    const items = Array.from({length:200}, (_,i) => ({nodeIdentity:'roll', key:[String(start+i)], childPathname:null, stepCacheKey:`k/${start+i}`, resultCid:null, state:'running'}))
    return {ok:true, json:async () => ({data: {runPage: {items, total:400, next:variables.after ? null : 'page-2'}}})}
  }))
  const table = buildLevel('job', overviewOf()).steps.find(s => s.id === 'roll')!
  const levelState = aLevelState()
  const render = (version: string) => root.render(<LevelStateContext.Provider value={levelState}>
    <OpenEntityContext.Provider value={recordingOpener().opener}>
      <StepPanel jobId="job" step={{...table, version}} view={{kind: 'renderer', id: 'custom'}} />
    </OpenEntityContext.Provider>
  </LevelStateContext.Provider>)
  try {
    await act(async () => render('0'))
    for (let revision=1; revision<=4; revision++) {
      await act(async () => {await vi.advanceTimersByTimeAsync(1500)})
      await act(async () => render(String(revision)))
    }
    expect(host.querySelector('[data-testid="custom-result"]')?.textContent).toBe('400 loaded')
    expect(signals.every(signal => !signal.aborted)).toBe(true)
  } finally {await act(async () => root.unmount()); vi.unstubAllGlobals(); vi.useRealTimers()}
})
