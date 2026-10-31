// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {expect, it, vi} from 'vitest'
import {StepPanel} from '../src/components/StepPanel'
import {OpenEntityContext} from '../src/lib/openEntity'
import {LevelStateContext} from '../src/lib/levelState'
import {buildLevel} from '../src/model/level'
import {base64Cbor, inProgress, levelState, overviewOf, recordingOpener} from './fixtures'

vi.mock('fxtr-view-shell-util', async importOriginal => ({
  ...await importOriginal<typeof import('fxtr-view-shell-util')>(),
  useRenderers: () => [], chosenRenderer: () => null, RenderInvocation: () => null,
}))

it.each(['failed', 'running', 'completed'])('shows a selected %s invocation in its step panel', async state => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div')
  const root = createRoot(host)
  // Distinct jobs keep the shared live pin cache independent between cases.
  const jobId = `job-${state}`
  const spawn = {...inProgress().spawns[1], state, resultCid: state === 'completed' ? 'result' : null}
  const table = buildLevel(jobId, overviewOf(inProgress({spawns: [spawn]}))).steps.find(s => s.id === 'roll')!
  const queries: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    const {query, variables} = JSON.parse(init.body)
    queries.push(query)
    let data: unknown
    if (query.includes('runPage')) {
      expect(variables.selection).toEqual({seed: 'b'})
      data = {runPage: {items: variables.unfinished && state === 'completed' ? [] : [spawn],
        total: variables.unfinished && state === 'completed' ? 0 : 1, next: null}}
    } else if (query.includes('outputPage')) {
      data = {outputPage: {type: table.type, rows: [{key: ['b'], value: base64Cbor('full scalar detail')}], offset: 0, total: 1, hasMore: false}}
    } else if (query.includes('step(')) {
      data = {step: {stepName: 'roll', requestCid: 'request', state, resultCid: null,
        logs: [{seq: 0, message: 'latest progress log', at: '2026-09-24T00:00:00Z'}],
        attempt: {state, startedAt: '2026-09-24T00:00:00Z', finishedAt: state === 'failed' ? '2026-09-24T00:00:01Z' : null,
          error: state === 'failed' ? {typeName: 'Error', message: 'important failure', traceback: ''} : null}}}
    } else throw new Error(`unexpected query: ${query}`)
    return {ok: true, json: async () => ({data})}
  }))
  try {
    await act(async () => root.render(<LevelStateContext.Provider value={levelState({slice: {seed: 'b'}})}>
      <OpenEntityContext.Provider value={recordingOpener().opener}>
        <StepPanel jobId={jobId} step={table} />
      </OpenEntityContext.Provider>
    </LevelStateContext.Provider>))
    if (state === 'completed') {
      expect(host.textContent).toContain('full scalar detail')
      expect(host.querySelector('table')).toBeNull()
    } else {
      expect(host.textContent).toContain('latest progress log')
      expect(host.textContent).toContain(state === 'failed' ? 'important failure' : 'output appears when the step finishes')
      expect(queries.some(query => query.includes('outputPage'))).toBe(false)
    }
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
