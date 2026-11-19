// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {expect, it, vi} from 'vitest'
vi.mock('../src/api/live', async (original) => ({
  ...await original<typeof import('../src/api/live')>(),
  useLivePolling: () => {},
  pokeLive: vi.fn(),
  useOverviewSnapshot: () => {throw new Promise(()=>{})},
}))
import {dispatchRegistry} from 'fxtr-view-shell-util'
import {App} from '../src/App'
import {pokeLive} from '../src/api/live'
import {configureViewer, LOCAL_CONFIG} from '../src/config'
it.each(['', '#job=job-a'])('keeps the selected job when a newer job appears (initial hash: %s)', async hash => {
  vi.useFakeTimers(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  window.history.replaceState(null, '', '/' + hash)
  const host=document.createElement('div'); const root=createRoot(host)
  let newest='job-a'
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    const {query, variables}=JSON.parse(init.body)
    const id=query.includes('jobSummary(') ? variables.id : newest
    const job={id, workflowName:'example', root:'root', state:'succeeded', failed:false, createdAt:'2026-09-24T00:00:00Z'}
    return {ok:true,json:async()=>({data:query.includes('jobSummary(')?{jobSummary:job}:{jobSummaries:{items:[job],next:null}}})}
  }))
  try {
    await act(async()=>root.render(<App views={{registry: dispatchRegistry([]), problem: null}}/>))
    expect(window.location.hash).toBe('#job=job-a')
    newest='job-b'
    await act(async()=>{await vi.advanceTimersByTimeAsync(30_000)})
    expect(window.location.hash).toBe('#job=job-a')
  } finally {await act(async()=>root.unmount());window.history.replaceState(null, '', '/');vi.unstubAllGlobals();vi.useRealTimers()}
})

it.each(['stopped', 'succeeded'])('refreshes a hosted %s job and its watched records without reloading the page', async state => {
  vi.useFakeTimers(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  window.history.replaceState(null, '', '/#job=job-a')
  const host=document.createElement('div'); const root=createRoot(host)
  vi.mocked(pokeLive).mockClear()
  const fetched=vi.fn(async (_url: string, init: RequestInit) => {
    const {variables}=JSON.parse(init.body as string)
    const job={id: variables.id, workflowName:'example', root:'root', state, failed:false, createdAt:'2026-09-24T00:00:00Z'}
    return {ok:true,json:async()=>({data:{jobSummary:job}})}
  })
  vi.stubGlobal('fetch', fetched)
  configureViewer({...LOCAL_CONFIG, eventsUrl: null})
  try {
    await act(async()=>root.render(<App views={{registry: dispatchRegistry([]), problem: null}}/>))
    expect(fetched).toHaveBeenCalledTimes(1)
    expect(pokeLive).not.toHaveBeenCalled()
    state = 'succeeded'
    await act(async()=>{await vi.advanceTimersByTimeAsync(30_000)})
    expect(fetched).toHaveBeenCalledTimes(2)
    expect(pokeLive).toHaveBeenCalledTimes(1)
    await act(async()=>{await vi.advanceTimersByTimeAsync(30_000)})
    expect(fetched).toHaveBeenCalledTimes(3)
    expect(pokeLive).toHaveBeenCalledTimes(2)
    expect(window.location.hash).toBe('#job=job-a')
  } finally {configureViewer(LOCAL_CONFIG);await act(async()=>root.unmount());window.history.replaceState(null, '', '/');vi.unstubAllGlobals();vi.useRealTimers()}
})
