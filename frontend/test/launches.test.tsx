// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('../src/api/live', async (original) => ({
  ...(await original<typeof import('../src/api/live')>()),
  useLivePolling: () => {},
  useOverviewSnapshot: () => {
    throw new Promise(() => {})
  },
}))
import { dispatchRegistry } from 'fxtr-view-shell-util'
import { toast } from 'sonner'
import { App } from '../src/App'
import { connectViewerEvents, type EventSocket } from '../src/api/events'
import { liveStatus, overviewEntry, startPolling, stopPolling } from '../src/api/live'
import type { JobSummary } from '../src/types'
import { inProgress, overviewOf } from './fixtures'

/** The events socket; the test sends what the server would. */
class Socket implements EventSocket {
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  close() {}
}

let host: HTMLDivElement
let root: Root
let socket: Socket
let disconnect: () => void
let jobState: JobSummary['state']
let invocation: ReturnType<typeof inProgress>

/** Waits out what follows an event: the job's summary, sonner's toast, its leaving. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(400)
  })
}

/** The page, with no toasts: sonner's toasts outlive a toaster, as they would not a page. */
async function render() {
  await act(async () => toast.dismiss())
  root = createRoot(host)
  await act(async () => root.render(<App views={{ registry: dispatchRegistry([]), problem: null }} />))
  await settle()
}

beforeEach(async () => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  window.history.replaceState(null, '', '/')
  sessionStorage.clear()
  jobState = 'running'
  invocation = inProgress()
  // Every job is found, named for its id; job-a is the newest.
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      const { query, variables } = JSON.parse(String(init.body))
      if (query.includes('levelOverview')) {
        return { ok: true, json: async () => ({ data: { levelOverview: overviewOf(invocation, jobState) } }) }
      }
      const summary = (id: string) => ({
        id,
        workflowName: `wf-${id}`,
        root: 'root',
        state: jobState,
        failed: jobState === 'stopped',
        createdAt: '2026-10-06T00:00:00Z',
      })
      const data = query.includes('jobSummary(')
        ? { jobSummary: summary(variables.id) }
        : { jobSummaries: { items: [summary('job-a')], next: null } }
      return { ok: true, json: async () => ({ data }) }
    }),
  )
  host = document.createElement('div')
  document.body.append(host)
  disconnect = connectViewerEvents(() => (socket = new Socket()))
  await render()
})

afterEach(async () => {
  await act(async () => toast.dismiss())
  await act(async () => root.unmount())
  disconnect()
  host.remove()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

async function launch(jobId: string, resumed = false) {
  const event = { type: 'job', jobId, workflowName: `wf-${jobId}`, resumed }
  await act(async () => socket.onmessage?.(new MessageEvent('message', { data: JSON.stringify(event) })))
  await settle()
}

function toasts(): string[] {
  return [...host.querySelectorAll('[data-sonner-toast]:not([data-removed="true"])')].map(
    (toast) => toast.textContent ?? '',
  )
}

async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(
    (b) => b.textContent === label || b.getAttribute('aria-label') === label,
  )
  if (!button) throw new Error(`no "${label}" button among: ${toasts().join(' | ')}`)
  await act(async () => button.click())
  await settle()
}

it('a page that follows launches switches to each one, says why, and can go back', async () => {
  expect(window.location.hash).toBe('#job=job-a')
  await launch('job-b')
  expect(window.location.hash).toBe('#job=job-b')
  expect(toasts()).toEqual([
    'showing the new jobwf-job-b was just launched, so this tab switched to itgo back',
  ])
  await click('go back')
  expect(window.location.hash).toBe('#job=job-a')
  expect(toasts()).toEqual([])

  // Going back is a pick: the page stays on its job, and offers the next launch instead.
  await launch('job-c')
  expect(window.location.hash).toBe('#job=job-a')
  expect(toasts()).toEqual(['a new job was launchedwf-job-c · this tab stays on the job you pickedshow'])
  await click('show')
  expect(window.location.hash).toBe('#job=job-c')

  // Showing it follows launches again.
  await launch('job-d')
  expect(window.location.hash).toBe('#job=job-d')
})

it('a page where someone picked a job keeps it, across a reload', async () => {
  // Changing the address's #job= picks that job.
  window.history.replaceState(null, '', '/#job=job-x')
  await act(async () => window.dispatchEvent(new HashChangeEvent('hashchange')))
  await settle()
  await launch('job-y', true)
  expect(window.location.hash).toBe('#job=job-x')
  expect(toasts()).toEqual(['a job was resumedwf-job-y · this tab stays on the job you pickedshow'])

  await act(async () => root.unmount())
  await render()
  await launch('job-z')
  expect(window.location.hash).toBe('#job=job-x')
})

it('launches in quick succession update one toast, which goes back to the job before them', async () => {
  await launch('job-b')
  await launch('job-c', true)
  expect(window.location.hash).toBe('#job=job-c')
  expect(toasts()).toEqual([
    'showing the resumed jobwf-job-c was just resumed, so this tab switched to itgo back',
  ])
  await click('go back')
  expect(window.location.hash).toBe('#job=job-a')
})

it('a launch of the job on screen does not navigate or raise a toast', async () => {
  await launch('job-a')
  expect(window.location.hash).toBe('#job=job-a')
  expect(toasts()).toEqual([])
})

it('refreshes a picked job that resumes and finishes before the next summary poll', async () => {
  // Picking the current job keeps this tab in place for other jobs' announcements.
  await act(async () => window.dispatchEvent(new HashChangeEvent('hashchange')))
  jobState = 'stopped'
  invocation = inProgress({ state: 'interrupted', spawns: [], attempts: [] })
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

  // Watch real cached records while the surrounding job view stays suspended in this fixture.
  const entry = overviewEntry('job-a', 'root')
  entry.watchers++
  try {
    await act(async () => startPolling('job-a'))
    await settle()
    expect(entry.value?.version).toBe('stopped')
    expect(liveStatus().polling).toBe(false)

    jobState = 'succeeded'
    invocation = { ...invocation, state: 'completed', resultCid: 'resumed-result' }
    await launch('job-b')
    expect(window.location.hash).toBe('#job=job-a')
    expect(entry.value?.version).toBe('stopped')
    await click('Close toast')

    await launch('job-a', true)
    expect(entry.value?.version).toBe('succeeded')
    expect(entry.value?.invocation.resultCid).toBe('resumed-result')
    expect(window.location.hash).toBe('#job=job-a')
    expect(toasts()).toEqual([])
  } finally {
    await act(async () => stopPolling('job-a'))
    entry.watchers--
  }
})
