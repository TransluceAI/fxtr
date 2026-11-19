// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { fetchJobSummariesByIds } from '../src/api/graphql'
import { useJobSummaries } from '../src/components/jobPickerQueries'
import type { JobSummary } from '../src/types'

vi.mock('../src/api/graphql', () => ({ fetchJobSummariesByIds: vi.fn() }))

const job = (id: string, state: JobSummary['state'] = 'running'): JobSummary => ({
  id, workflowName: 'sweep', root: 'root', createdAt: '2026-11-01T00:00:00Z',
  state, failed: false, failedSteps: [], inputs: [], changedInputs: [], latest: true,
})

interface Request {
  ids: readonly string[]
  signal: AbortSignal
  resolve: (jobs: JobSummary[]) => void
  reject: (error: Error) => void
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

/** Requests settle only when the test answers them, including out of order. */
function reader() {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const requests: Request[] = []
  vi.mocked(fetchJobSummariesByIds).mockImplementation((ids, signal) => new Promise((resolve, reject) => {
    requests.push({ ids, signal: signal!, resolve, reject })
    signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
  }))
  let current: ReturnType<typeof useJobSummaries>
  function Reader({ ids, preview, open }: { ids: string[]; preview: string | null; open: boolean }) {
    current = useJobSummaries(ids, preview, open)
    return null
  }
  const root = createRoot(document.body.appendChild(document.createElement('div')))
  return {
    requests,
    get current() { return current },
    show: (ids: string[], preview: string | null = null, open = true) => act(async () => root.render(<Reader ids={ids} preview={preview} open={open} />)),
    finish: () => act(async () => root.unmount()),
  }
}

it('keeps the active batch, prioritizes previews, and drops obsolete queued demand', async () => {
  const read = reader()
  const original = Array.from({ length: 60 }, (_, index) => `job-${index}`)
  await read.show(original)
  expect(read.requests).toHaveLength(1)
  expect(read.requests[0].ids).toEqual(original.slice(0, 50))

  // Scroll elsewhere and preview another row before the background request finishes.
  await read.show(['job-550'], 'job-600')
  expect(read.requests).toHaveLength(2)
  expect(read.requests[0].signal.aborted).toBe(false)
  expect(read.requests[1].ids).toEqual(['job-600'])
  await act(async () => read.requests[1].resolve([job('job-600')]))
  expect(read.current.summaries.has('job-600')).toBe(true)
  await act(async () => read.requests[0].resolve(original.slice(0, 50).map((id) => job(id))))
  expect(read.requests[2].ids).toEqual(['job-550'])
  expect(read.requests.flatMap((request) => request.ids)).not.toContain('job-50')
  await act(async () => read.requests[2].resolve([job('job-550')]))

  await read.show(original.slice(0, 20))
  expect(read.requests).toHaveLength(3) // Scrolling back uses the cache.
  await read.finish()
})

it('does not let a slow batch overwrite the preview response for the same job', async () => {
  const read = reader()
  await read.show(['a', 'b'])
  await read.show(['a', 'b'], 'b')
  expect(read.requests[1].ids).toEqual(['b'])
  await act(async () => read.requests[1].resolve([job('b', 'succeeded')]))
  await act(async () => read.requests[0].resolve([job('a'), job('b')]))
  expect(read.current.summaries.get('b')!.state).toBe('succeeded')
  await read.finish()
})

it('keeps cached values on reopening while revalidating only requested jobs', async () => {
  const read = reader()
  await read.show(['a', 'b'])
  await act(async () => read.requests[0].resolve([job('a'), job('b')]))
  await read.show([], null, false)
  await read.show(['b'])
  expect(read.current.summaries.get('b')!.state).toBe('running')
  expect(read.requests[1].ids).toEqual(['b'])
  await act(async () => read.requests[1].resolve([job('b', 'succeeded')]))
  expect(read.current.summaries.get('b')!.state).toBe('succeeded')
  expect(read.requests).toHaveLength(2)
  await read.finish()
})

it('cancels superseded previews and all requests when the picker closes', async () => {
  const read = reader()
  await read.show(['a'], 'b')
  await read.show(['a'], 'c')
  expect(read.requests[0].signal.aborted).toBe(false)
  expect(read.requests[1].signal.aborted).toBe(true)
  expect(read.requests[2].ids).toEqual(['c'])
  await read.show([], null, false)
  expect(read.requests.every((request) => request.signal.aborted)).toBe(true)
  expect(read.current.error).toBeNull()
  await read.finish()
})

it('retries failed reads without discarding cached rows', async () => {
  const read = reader()
  await read.show(['a'])
  await act(async () => read.requests[0].resolve([job('a')]))
  await read.show(['b'])
  await act(async () => read.requests[1].reject(new Error('offline')))
  expect(String(read.current.error)).toContain('offline')
  expect(read.current.summaries.has('a')).toBe(true)
  await act(async () => read.current.reload())
  expect(read.requests[2].ids).toEqual(['b'])
  await act(async () => read.requests[2].resolve([job('b')]))
  expect(read.current.error).toBeNull()
  expect([...read.current.summaries.keys()]).toEqual(['a', 'b'])
  await read.finish()
})
