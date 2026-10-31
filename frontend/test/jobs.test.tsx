// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {expect, it, vi} from 'vitest'
vi.mock('../src/api/live', async (original) => ({
  ...await original<typeof import('../src/api/live')>(),
  useLivePolling: () => {},
  useOverviewSnapshot: () => {throw new Promise(()=>{})},
}))
import {dispatchRegistry} from 'fxtr-view-shell-util'
import {App} from '../src/App'
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
